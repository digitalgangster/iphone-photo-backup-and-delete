#!/usr/bin/env python3
"""
iPhone Photo Extractor - engine (worker process).

Runs in its own process, started and watched by extractor.py. Talks to the supervisor by printing one
JSON line per second to stdout (the heartbeat). If the heartbeat stops, the supervisor kills this
process and starts a fresh one; because every completed file is recorded in a SQLite ledger only
after it is safely on disk, nothing finished is ever redone or lost.

SAFETY: the phone is accessed only through ReadOnlyPhone (list, file info, open-for-read, read, close).
Nothing on the phone is ever written, renamed or deleted. On the PC, existing files are never
overwritten: name clashes get a " (2)" suffix and identical files are recognized and reused.
"""

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import logging.handlers
import os
import shutil
import sqlite3
import struct
import sys
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path

ENGINE_VERSION = "1.1"
CHUNK = 1024 * 1024                  # 1 MB reads (the size measured stable on this phone)
OP_TIMEOUT = 30                      # any single phone operation slower than this counts as a stall
STAT_CONCURRENCY = 8
MAX_ATTEMPTS = 5
FREE_SPACE_MARGIN = 2 * 1024 ** 3    # keep 2 GB free on the destination drive
CORE_DATA_EPOCH = 978307200          # 2001-01-01 in Unix time
PAIR_RETRY_SECONDS = 3

EXIT_COMPLETE = 0
EXIT_RESTART = 1
EXIT_FATAL = 3
EXIT_DISK_FULL = 4

PHOTO_EXT = {"HEIC", "HEIF", "JPG", "JPEG", "PNG", "DNG", "GIF", "TIF", "TIFF", "WEBP", "BMP", "AVIF", "PSD"}
VIDEO_EXT = {"MOV", "MP4", "M4V", "HEVC", "3GP", "AVI"}
BMFF_EXT = {"HEIC", "HEIF", "AVIF", "MOV", "MP4", "M4V", "3GP"}
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

log = logging.getLogger("extractor")


class PhoneStall(Exception):
    pass


class DiskFull(Exception):
    pass


class FatalError(Exception):
    pass


def emit(obj):
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def ext_of(name):
    return name.rsplit(".", 1)[-1].upper() if "." in name else ""


def stem_of(name):
    return name.rsplit(".", 1)[0] if "." in name else name


# ============================================================================ phone access (read-only)
class ReadOnlyPhone:
    """The only object that talks to the phone: list, info, open-for-READ, read, close. Nothing else."""

    def __init__(self, afc):
        self._afc = afc

    async def _guard(self, coro, what):
        try:
            return await asyncio.wait_for(coro, OP_TIMEOUT)
        except asyncio.TimeoutError:
            raise PhoneStall(f"phone did not respond for {OP_TIMEOUT}s ({what})")

    async def listdir(self, path):
        return await self._guard(self._afc.listdir(path), f"list {path}")

    async def stat(self, path):
        return await self._guard(self._afc.stat(path), f"info {path}")

    async def open_read(self, path):
        return await self._guard(self._afc.fopen(path, "r"), f"open {path}")

    async def read(self, handle, size):
        return await self._guard(self._afc.fread(handle, size), "read")

    async def close(self, handle):
        try:
            await self._guard(self._afc.fclose(handle), "close")
        except Exception:
            pass


# ============================================================================ file checks & helpers
def _tail(f, size, n):
    f.seek(max(0, size - n))
    return f.read(n)


def _walk_boxes(f, size):
    """Walk ISO-BMFF / QuickTime top-level boxes. Returns (types, ok, reason)."""
    off, types = 0, []
    while off < size:
        f.seek(off)
        hdr = f.read(16)
        if len(hdr) < 8:
            return types, False, f"truncated box header at byte {off:,}"
        bsize, btype = struct.unpack(">I4s", hdr[:8])
        if bsize == 1:
            if len(hdr) < 16:
                return types, False, f"truncated large box at byte {off:,}"
            bsize = struct.unpack(">Q", hdr[8:16])[0]
        elif bsize == 0:
            bsize = size - off
        if bsize < 8:
            return types, False, f"invalid box size at byte {off:,}"
        types.append(btype)
        off += bsize
    if off != size:
        return types, False, f"file ends {off - size:,} bytes before its last section does (incomplete)"
    return types, True, ""


BMFF_BOXES = (b"ftyp", b"wide", b"mdat", b"moov", b"free", b"skip", b"pnot", b"meta")


def sniff_kind(head):
    """What a file really is, judged by its first bytes (names can lie: 'image.png' is often a JPEG)."""
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:4] == b"GIF8":
        return "GIF"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[4:8] in BMFF_BOXES:
        return "BMFF"
    return None


def verify_structure(path, ext):
    """Cheap completeness check by what the file actually contains. Returns (ok, reason)."""
    size = os.path.getsize(path)
    if size == 0:
        return False, "empty file"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
            kind = sniff_kind(head)
            if kind is None:
                if ext in ("JPG", "JPEG", "PNG", "GIF", "WEBP") or ext in BMFF_EXT:
                    return False, f"not a recognizable {ext} file"
                return True, ""
            if kind == "JPEG":
                if b"\xff\xd9" not in _tail(f, size, 65536):
                    return False, "JPEG end marker missing (incomplete)"
            elif kind == "BMFF":
                types, ok, why = _walk_boxes(f, size)
                if not ok:
                    return False, why
                if ext in VIDEO_EXT and b"moov" not in types:
                    return False, "video index (moov) missing"
            elif kind == "PNG":
                if b"IEND" not in _tail(f, size, 4096):
                    return False, "PNG end chunk missing (incomplete)"
            elif kind == "GIF":
                if b"\x3b" not in _tail(f, size, 1024):
                    return False, "GIF end missing (incomplete)"
            elif kind == "WEBP":
                if struct.unpack("<I", head[4:8])[0] + 8 > size:
                    return False, "WEBP shorter than its header says (incomplete)"
    except OSError as e:
        return False, f"could not re-read the copy: {e}"
    return True, ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    bad = '<>:"/\\|?*'
    out = "".join("_" if (c in bad or ord(c) < 32) else c for c in name).strip().rstrip(". ")
    if not out:
        out = "unnamed"
    if stem_of(out).upper() in WIN_RESERVED:
        out = "_" + out
    return out[:180]


def choose_name(orig_name, phone_name):
    phone_ext = phone_name.rsplit(".", 1)[-1] if "." in phone_name else ""
    if orig_name:
        orig_name = safe_name(orig_name)
        if ext_of(orig_name) == phone_ext.upper():
            return orig_name
        return f"{stem_of(orig_name)}.{phone_ext}" if phone_ext else orig_name
    return safe_name(phone_name)


def set_file_times(path, ts):
    """Set created + modified (+ accessed) time to the photo's capture time."""
    if not ts:
        return
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.restype = wintypes.HANDLE
            k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            v = int((ts + 11644473600) * 10_000_000)
            ft = wintypes.FILETIME(v & 0xFFFFFFFF, v >> 32)
            h = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x80, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
            if h in (None, wintypes.HANDLE(-1).value):
                raise OSError(ctypes.get_last_error(), "CreateFileW failed")
            try:
                k32.SetFileTime(h, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
            finally:
                k32.CloseHandle(h)
        else:
            os.utime(path, (ts, ts))
    except Exception as e:
        log.warning("could not set dates on %s: %s", path, e)
        try:
            os.utime(path, (ts, ts))
        except Exception:
            pass


def hide_folder(path):
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
        except Exception:
            pass


class Meter:
    def __init__(self, window=20):
        self.window = window
        self.samples = deque()

    def add(self, n):
        now = time.monotonic()
        self.samples.append((now, n))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def rate(self):
        now = time.monotonic()
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if not self.samples:
            return 0.0
        span = max(now - self.samples[0][0], 1.0)
        return sum(n for _, n in self.samples) / span


# ============================================================================ ledger
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS assets (
    uuid TEXT PRIMARY KEY, dir TEXT, fname TEXT, kind INTEGER, orig_size INTEGER, orig_name TEXT,
    created REAL, trashed INTEGER, hidden INTEGER, on_phone INTEGER DEFAULT 0, in_library INTEGER DEFAULT 1,
    seen_run INTEGER);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, phone_path TEXT UNIQUE, size INTEGER, mtime REAL, birth REAL,
    uuid TEXT, role TEXT, dest_rel TEXT, dest_key TEXT UNIQUE,
    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, last_error TEXT, last_hash TEXT,
    sha256 TEXT, dup_of INTEGER, warning TEXT, done_at TEXT, seen_run INTEGER);
CREATE INDEX IF NOT EXISTS files_status ON files(status, attempts, id);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256, size);
CREATE INDEX IF NOT EXISTS files_uuid ON files(uuid, role);
"""


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def q(self, sql, args=()):
        return self.db.execute(sql, args)

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def meta(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r[0] if r else default

    def set_meta(self, k, v):
        self.q("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ============================================================================ engine
class Engine:
    def __init__(self, dest, retry_failed=False):
        self.dest = Path(dest)
        self.work = self.dest / ".extractor"
        self.tmp = self.work / "tmp"
        self.reports = self.dest / "_Export Reports"
        for p in (self.dest, self.work, self.tmp, self.work / "logs", self.work / "phone_db", self.reports):
            p.mkdir(parents=True, exist_ok=True)
        hide_folder(self.work)
        self._setup_logging()
        self._lock()
        self.ledger = Ledger(self.work / "transfer.db")
        for leftover in self.tmp.glob("*.part"):
            leftover.unlink(missing_ok=True)
        if retry_failed:
            n = self.ledger.q("UPDATE files SET status='pending', attempts=0, last_error=NULL "
                              "WHERE status='failed'").rowcount
            log.info("retry requested: %d failed files reset to pending", n)
        self.state = "starting"
        self.message = "Starting..."
        self.device = None
        self.current = None
        self.current_done = 0
        self.meter = Meter()
        self.fail_streak = 0
        self._lib_cache = (0.0, {})
        self.library_known = self.ledger.meta("library_known") == "1"
        self.run_no = int(self.ledger.meta("run_no", 0))

    # ---------------------------------------------------------------- infrastructure
    def _setup_logging(self):
        h = logging.handlers.RotatingFileHandler(self.work / "logs" / "extractor.log", maxBytes=5 * 1024 ** 2,
                                                 backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
        log.info("engine %s starting, destination %s", ENGINE_VERSION, self.dest)

    def _lock(self):
        self._lockf = open(self.work / "worker.lock", "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._lockf.seek(0)
                msvcrt.locking(self._lockf.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FatalError("Another copy of the extractor is already working on this folder.")

    def say(self, state, message):
        self.state, self.message = state, message
        log.info("%s: %s", state, message)

    def snapshot(self):
        L = self.ledger
        by = {s: (c, b or 0) for s, c, b in L.q(
            "SELECT status, COUNT(*), SUM(size) FROM files WHERE status!='gone' GROUP BY status")}
        total_files = sum(c for c, _ in by.values())
        total_bytes = sum(b for _, b in by.values())
        done_files = by.get("done", (0, 0))[0] + by.get("duplicate", (0, 0))[0]
        done_bytes = by.get("done", (0, 0))[1] + by.get("duplicate", (0, 0))[1] + self.current_done
        pending_bytes = max(by.get("pending", (0, 0))[1] - self.current_done, 0)
        speed = self.meter.rate()
        snap = {
            "t": "status", "state": self.state, "msg": self.message, "device": self.device,
            "total_files": total_files, "done_files": done_files,
            "failed": by.get("failed", (0, 0))[0], "duplicates": by.get("duplicate", (0, 0))[0],
            "pending": by.get("pending", (0, 0))[0],
            "total_bytes": total_bytes, "done_bytes": done_bytes, "pending_bytes": pending_bytes,
            "speed": speed, "eta": (pending_bytes / speed) if speed > 1024 else None,
            "current": self.current,
            "warnings": L.one("SELECT COUNT(*) FROM files WHERE warning IS NOT NULL AND status='done'")[0],
        }
        if self.library_known and time.monotonic() - self._lib_cache[0] < 5:
            snap.update(self._lib_cache[1])
        elif self.library_known:
            r = L.one("""SELECT
                    COUNT(*),
                    SUM(on_phone),
                    SUM(CASE WHEN on_phone=0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN EXISTS (SELECT 1 FROM files f WHERE f.uuid=a.uuid AND f.role='main'
                                          AND f.status IN ('done','duplicate')) THEN 1 ELSE 0 END)
                FROM assets a WHERE in_library=1 AND trashed=0""")
            lib = dict(library_items=r[0] or 0, library_on_phone=r[1] or 0,
                       not_on_phone=r[2] or 0, library_backed_up=r[3] or 0)
            self._lib_cache = (time.monotonic(), lib)
            snap.update(lib)
        return snap

    async def heartbeat(self):
        while True:
            try:
                emit(self.snapshot())
            except Exception as e:
                emit({"t": "status", "state": self.state, "msg": f"(status error: {e})"})
            await asyncio.sleep(1)

    # ---------------------------------------------------------------- connection
    async def connect(self):
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import (
            NotPairedError, PairingDialogResponsePendingError, PasswordRequiredError, UserDeniedPairingError,
        )
        from pymobiledevice3.lockdown import create_using_usbmux
        while True:
            try:
                usb = [d for d in await usbmux.list_devices() if d.is_usb]
                if not usb:
                    self.say("waiting", "Waiting for the iPhone. Plug it in with the cable and unlock it.")
                else:
                    lockdown = await create_using_usbmux(serial=usb[0].serial, connection_type="USB",
                                                         pair_timeout=60)
                    v = lockdown.all_values
                    name = getattr(lockdown, "display_name", None) or v.get("ProductType")
                    self.device = f"{name} - iOS {v.get('ProductVersion')}"
                    return lockdown
            except PasswordRequiredError:
                self.say("waiting", "The iPhone is locked. Unlock it to continue.")
            except (PairingDialogResponsePendingError, NotPairedError):
                self.say("waiting", "Tap 'Trust' on the iPhone and enter your passcode.")
            except UserDeniedPairingError:
                self.say("waiting", "'Don't Trust' was tapped. Unplug, plug back in, and tap 'Trust'.")
            except ConnectionRefusedError:
                self.say("waiting", "Apple Mobile Device Service is not running. Install 'Apple Devices' "
                                    "from the Microsoft Store and restart the PC.")
            except Exception as e:
                self.say("waiting", f"Connecting to the iPhone... ({type(e).__name__})")
            await asyncio.sleep(PAIR_RETRY_SECONDS)

    # ---------------------------------------------------------------- indexing
    async def _copy_remote(self, phone, remote, local):
        part = local.with_suffix(local.suffix + ".part")
        h = await phone.open_read(remote)
        try:
            with open(part, "wb") as f:
                while True:
                    data = await phone.read(h, CHUNK)
                    if not data:
                        break
                    f.write(data)
        finally:
            await phone.close(h)
        os.replace(part, local)

    async def load_library(self, phone, run):
        """Copy Photos.sqlite (read-only) and load every library item into the ledger."""
        from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError
        dbdir = self.work / "phone_db"
        for old in dbdir.glob("Photos.sqlite*"):
            old.unlink(missing_ok=True)
        try:
            for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
                try:
                    await self._copy_remote(phone, f"/PhotoData/{name}", dbdir / name)
                except AfcFileNotFoundError:
                    if name == "Photos.sqlite":
                        raise
        except (AfcFileNotFoundError, AfcException) as e:
            log.warning("photo library database not readable (%s); continuing without it", e)
            return None

        try:
            src = sqlite3.connect(str(dbdir / "Photos.sqlite"))
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            at = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"
            ac = {r[1] for r in src.execute(f"PRAGMA table_info({at})")}
            xt = "ZADDITIONALASSETATTRIBUTES"
            xc = {r[1] for r in src.execute(f"PRAGMA table_info({xt})")} if xt in tables else set()

            def a(col, alias):
                return f"a.{col} AS {alias}" if col in ac else f"NULL AS {alias}"

            cols = ["a.Z_PK", a("ZUUID", "uuid"), a("ZDIRECTORY", "dir"), a("ZFILENAME", "fname"),
                    a("ZKIND", "kind"), a("ZDATECREATED", "created"), a("ZTRASHEDSTATE", "trashed"),
                    a("ZHIDDEN", "hidden")]
            join = ""
            if "ZASSET" in xc:
                cols.append("x.ZORIGINALFILESIZE" if "ZORIGINALFILESIZE" in xc else "NULL")
                cols.append("x.ZORIGINALFILENAME" if "ZORIGINALFILENAME" in xc else "NULL")
                join = f"LEFT JOIN {xt} x ON x.ZASSET = a.Z_PK"
            else:
                cols += ["NULL", "NULL"]
            rows = src.execute(f"SELECT {', '.join(cols)} FROM {at} a {join}")
        except Exception as e:
            log.warning("photo library database could not be parsed (%s); continuing without it", e)
            return None

        lib = {"main": {}, "trashed": set(), "dirs": set()}
        L = self.ledger
        L.q("BEGIN")
        try:
            for pk, uuid, d, fname, kind, created, trashed, hidden, osize, oname in rows:
                uuid = uuid or f"pk{pk}"
                ts = (CORE_DATA_EPOCH + float(created)) if created is not None else None
                L.q("""INSERT INTO assets(uuid,dir,fname,kind,orig_size,orig_name,created,trashed,hidden,
                                          in_library,seen_run)
                       VALUES(?,?,?,?,?,?,?,?,?,1,?)
                       ON CONFLICT(uuid) DO UPDATE SET dir=excluded.dir, fname=excluded.fname, kind=excluded.kind,
                         orig_size=excluded.orig_size, orig_name=excluded.orig_name, created=excluded.created,
                         trashed=excluded.trashed, hidden=excluded.hidden, in_library=1, seen_run=excluded.seen_run""",
                    (uuid, d, fname, kind, osize, oname, ts, 1 if trashed == 1 else 0, 1 if hidden == 1 else 0, run))
                if not d or not fname:
                    continue
                path = f"{d}/{fname}".upper()
                if trashed == 1:
                    lib["trashed"].add(path)
                else:
                    lib["main"][path] = uuid
                    if not d.upper().startswith("DCIM/"):
                        lib["dirs"].add(d)
            L.q("UPDATE assets SET in_library=0 WHERE seen_run IS NOT ?", (run,))
            L.q("COMMIT")
        except Exception:
            L.q("ROLLBACK")
            raise
        finally:
            src.close()
        log.info("library: %d items, %d stored outside DCIM in %d folders",
                 len(lib["main"]), sum(1 for p in lib["main"] if not p.startswith("DCIM/")), len(lib["dirs"]))
        return lib

    async def index(self, phone):
        from pymobiledevice3.exceptions import AfcFileNotFoundError
        run = self.run_no + 1
        self.say("indexing", "Reading the photo library database (read-only)...")
        lib = await self.load_library(phone, run)

        self.say("indexing", "Finding every photo and video on the phone...")
        dirs = []
        for name in sorted(await phone.listdir("/DCIM")):
            st = await phone.stat(f"/DCIM/{name}")
            if st.get("st_ifmt") == "S_IFDIR":
                dirs.append(f"DCIM/{name}")
        if lib:
            dirs += sorted(lib["dirs"])
        main = lib["main"] if lib else {}
        trashed = lib["trashed"] if lib else set()
        sem = asyncio.Semaphore(STAT_CONCURRENCY)

        async def info(path):
            async with sem:
                return await phone.stat(path)

        L = self.ledger
        found = 0
        for di, d in enumerate(dirs, 1):
            try:
                names = await phone.listdir(f"/{d}")
            except AfcFileNotFoundError:
                continue
            stats = await asyncio.gather(*(info(f"/{d}/{n}") for n in names))
            entries = [(n, st) for n, st in zip(names, stats) if st.get("st_ifmt") == "S_IFREG"]
            mains_by_stem = {}
            trashed_stems = set()
            for n, _ in entries:
                key = f"{d}/{n}".upper()
                if key in main:
                    mains_by_stem[stem_of(n).upper()] = main[key]
                elif key in trashed:
                    trashed_stems.add(stem_of(n).upper())
            L.q("BEGIN")
            try:
                for n, st in entries:
                    key = f"{d}/{n}".upper()
                    stem = stem_of(n).upper()
                    if key in trashed or stem in trashed_stems:
                        continue
                    uuid, role = main.get(key), "main"
                    if not uuid:
                        uuid = mains_by_stem.get(stem) or mains_by_stem.get(stem.split("_")[0])
                        role = ("live_video" if ext_of(n) in VIDEO_EXT else "sidecar") if uuid else "unmatched"
                    size = int(st.get("st_size", 0))
                    mt = st["st_mtime"].timestamp() if st.get("st_mtime") else None
                    bt = st["st_birthtime"].timestamp() if st.get("st_birthtime") else None
                    L.q("""INSERT INTO files(phone_path,size,mtime,birth,uuid,role,seen_run) VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(phone_path) DO UPDATE SET
                             status = CASE WHEN files.status IN ('done','duplicate') AND files.size != excluded.size
                                             THEN 'pending'
                                           WHEN files.status='gone' THEN 'pending' ELSE files.status END,
                             attempts = CASE WHEN files.size != excluded.size THEN 0 ELSE files.attempts END,
                             dest_rel = CASE WHEN files.status IN ('done','duplicate') AND files.size != excluded.size
                                             THEN NULL ELSE files.dest_rel END,
                             dest_key = CASE WHEN files.status IN ('done','duplicate') AND files.size != excluded.size
                                             THEN NULL ELSE files.dest_key END,
                             size=excluded.size, mtime=excluded.mtime, birth=excluded.birth,
                             uuid=excluded.uuid, role=excluded.role, seen_run=excluded.seen_run""",
                        (f"{d}/{n}", size, mt, bt, uuid, role, run))
                    found += 1
                L.q("COMMIT")
            except Exception:
                L.q("ROLLBACK")
                raise
            self.say("indexing", f"Finding every photo and video... folder {di}/{len(dirs)}, {found:,} files")

        L.q("UPDATE files SET status='gone' WHERE status='pending' AND seen_run IS NOT ?", (run,))
        if lib:
            L.q("""UPDATE assets SET on_phone = EXISTS (SELECT 1 FROM files f WHERE f.uuid=assets.uuid
                   AND f.role='main' AND f.seen_run=?)""", (run,))
        self.assign_names()
        self.run_no = run
        L.set_meta("run_no", run)
        self.library_known = lib is not None
        L.set_meta("library_known", "1" if lib else "0")
        log.info("index complete: %d files on phone", found)

    def _claim(self, fid, folder, stem, ext, check_disk=False):
        n = 1
        while True:
            name = f"{stem}.{ext}" if n == 1 else f"{stem} ({n}).{ext}"
            if not ext:
                name = stem if n == 1 else f"{stem} ({n})"
            rel = f"{folder}/{name}"
            if check_disk and (self.dest / rel).exists():
                n += 1
                continue
            try:
                self.ledger.q("UPDATE files SET dest_rel=?, dest_key=? WHERE id=?", (rel, rel.lower(), fid))
                return rel
            except sqlite3.IntegrityError:
                n += 1

    @staticmethod
    def _folder(ts):
        if not ts or ts < 0:
            return "Unknown date"
        try:
            dt = datetime.fromtimestamp(ts)
            return f"{dt:%Y}/{dt:%m}" if dt.year >= 1990 else "Unknown date"
        except (OverflowError, OSError, ValueError):
            return "Unknown date"

    def assign_names(self):
        L = self.ledger
        L.q("BEGIN")
        try:
            mains = L.q("""SELECT f.id, f.phone_path, a.orig_name, COALESCE(a.created, f.birth, f.mtime)
                           FROM files f LEFT JOIN assets a ON a.uuid=f.uuid
                           WHERE f.dest_key IS NULL AND f.status NOT IN ('gone','duplicate') AND f.role='main'
                           ORDER BY 4, f.id""").fetchall()
            for fid, path, oname, ts in mains:
                name = choose_name(oname, path.rsplit("/", 1)[-1])
                self._claim(fid, self._folder(ts), stem_of(name), ext_of(name) and name.rsplit(".", 1)[-1])
            sibs = L.q("""SELECT f.id, f.phone_path, f.uuid, COALESCE(f.birth, f.mtime) FROM files f
                          WHERE f.dest_key IS NULL AND f.status NOT IN ('gone','duplicate')
                          AND f.role IN ('live_video','sidecar') ORDER BY f.id""").fetchall()
            for fid, path, uuid, ts in sibs:
                phone_name = path.rsplit("/", 1)[-1]
                ext = phone_name.rsplit(".", 1)[-1] if "." in phone_name else ""
                m = L.one("SELECT dest_rel FROM files WHERE uuid=? AND role='main' AND dest_rel IS NOT NULL", (uuid,))
                if m:
                    folder, mname = m[0].rsplit("/", 1)
                    self._claim(fid, folder, stem_of(mname), ext)
                else:
                    self._claim(fid, self._folder(ts), stem_of(safe_name(phone_name)), ext)
            rest = L.q("""SELECT id, phone_path, COALESCE(birth, mtime) FROM files
                          WHERE dest_key IS NULL AND status NOT IN ('gone','duplicate') ORDER BY id""").fetchall()
            for fid, path, ts in rest:
                phone_name = safe_name(path.rsplit("/", 1)[-1])
                ext = phone_name.rsplit(".", 1)[-1] if "." in phone_name else ""
                self._claim(fid, self._folder(ts), stem_of(phone_name), ext)
            L.q("COMMIT")
        except Exception:
            L.q("ROLLBACK")
            raise

    # ---------------------------------------------------------------- copying
    def check_space(self):
        need = self.ledger.one("SELECT COALESCE(SUM(size),0) FROM files WHERE status='pending'")[0]
        free = shutil.disk_usage(self.dest).free
        if need + FREE_SPACE_MARGIN > free:
            raise DiskFull(f"Not enough space on {self.dest.anchor}: need {need / 1024 ** 3:,.1f} GB, "
                           f"only {free / 1024 ** 3:,.1f} GB free. Choose a bigger drive.")

    def _fail(self, fid, attempts, msg, last_hash=None):
        attempts += 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        self.ledger.q("UPDATE files SET attempts=?, status=?, last_error=?, last_hash=COALESCE(?, last_hash) "
                      "WHERE id=?", (attempts, status, msg[:500], last_hash, fid))
        log.warning("file %d attempt %d %s: %s", fid, attempts, status, msg)

    async def transfer_all(self, phone):
        L = self.ledger
        while True:
            rows = L.q("""SELECT f.id, f.phone_path, f.size, f.dest_rel, f.attempts, f.last_hash,
                                 COALESCE(a.created, f.birth, f.mtime)
                          FROM files f LEFT JOIN assets a ON a.uuid=f.uuid
                          WHERE f.status='pending' AND f.dest_rel IS NOT NULL
                          ORDER BY f.attempts, f.id LIMIT 200""").fetchall()
            if not rows:
                return
            for row in rows:
                before = L.one("SELECT attempts, status FROM files WHERE id=?", (row[0],))
                await self.transfer_one(phone, *row)
                after = L.one("SELECT attempts, status FROM files WHERE id=?", (row[0],))
                if after and before and after[0] > before[0] and after[1] != "done":
                    self.fail_streak += 1
                    if self.fail_streak >= 10:
                        self.fail_streak = 0
                        raise PhoneStall("10 files in a row failed; resetting the connection")
                else:
                    self.fail_streak = 0

    async def transfer_one(self, phone, fid, phone_path, size, dest_rel, attempts, last_hash, ts):
        from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError
        L = self.ledger
        remote = "/" + phone_path
        part = self.tmp / f"{fid}.part"
        self.current = dest_rel
        self.current_done = 0
        self.say("copying", f"Copying {dest_rel}")

        free = shutil.disk_usage(self.dest).free
        if free < size + FREE_SPACE_MARGIN:
            raise DiskFull(f"The destination drive is nearly full ({free / 1024 ** 3:,.1f} GB free).")

        hasher = hashlib.sha256()
        got = 0
        try:
            try:
                h = await phone.open_read(remote)
            except AfcFileNotFoundError:
                L.q("UPDATE files SET status='gone', last_error='no longer on the phone' WHERE id=?", (fid,))
                return
            try:
                with open(part, "wb") as f:
                    while True:
                        data = await phone.read(h, CHUNK)
                        if not data:
                            break
                        f.write(data)
                        hasher.update(data)
                        got += len(data)
                        self.current_done = got
                        self.meter.add(len(data))
                        if got > size + CHUNK:
                            break
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                await phone.close(h)
            if got != size:
                part.unlink(missing_ok=True)
                self._fail(fid, attempts, f"incomplete read: got {got:,} of {size:,} bytes")
                return
            st2 = await phone.stat(remote)
            if int(st2.get("st_size", -1)) != size:
                part.unlink(missing_ok=True)
                self._fail(fid, attempts, "file changed on the phone during the copy")
                return
        except AfcFileNotFoundError:
            part.unlink(missing_ok=True)
            L.q("UPDATE files SET status='gone', last_error='no longer on the phone' WHERE id=?", (fid,))
            return
        except AfcException as e:
            part.unlink(missing_ok=True)
            await phone.stat("/DCIM")          # raises if the connection itself is broken -> reconnect
            self._fail(fid, attempts, f"phone refused the read: {e}")
            return
        except BaseException as e:
            part.unlink(missing_ok=True)
            if isinstance(e, Exception):
                self._fail(fid, attempts, f"connection problem: {type(e).__name__}: {e}")
            raise
        finally:
            self.current_done = 0

        digest = hasher.hexdigest()
        ext = ext_of(dest_rel)
        ok, why = verify_structure(part, ext)
        warning = None
        if not ok:
            if last_hash == digest:
                warning = f"copied byte-for-byte, but the file on the phone itself looks damaged: {why}"
            else:
                part.unlink(missing_ok=True)
                self._fail(fid, attempts, f"completeness check failed: {why}", last_hash=digest)
                return

        dup = L.one("SELECT id, dest_rel FROM files WHERE sha256=? AND size=? AND status='done' AND id!=? LIMIT 1",
                    (digest, size, fid))
        if dup and (self.dest / dup[1]).exists():
            part.unlink(missing_ok=True)
            L.q("""UPDATE files SET status='duplicate', dup_of=?, dest_rel=?, dest_key=NULL, sha256=?,
                   done_at=?, last_error=NULL WHERE id=?""",
                (dup[0], dup[1], digest, datetime.now().isoformat(timespec="seconds"), fid))
            return

        final = self.dest / dest_rel
        final.parent.mkdir(parents=True, exist_ok=True)
        adopted = False
        while final.exists():
            if final.stat().st_size == size and sha256_file(final) == digest:
                adopted = True
                part.unlink(missing_ok=True)
                break
            folder, name = dest_rel.rsplit("/", 1)
            L.q("UPDATE files SET dest_key=NULL WHERE id=?", (fid,))
            dest_rel = self._claim(fid, folder, stem_of(name), name.rsplit(".", 1)[-1] if "." in name else "",
                                   check_disk=True)
            final = self.dest / dest_rel
        if not adopted:
            os.replace(part, final)
        set_file_times(final, ts)
        L.q("""UPDATE files SET status='done', sha256=?, warning=?, done_at=?, last_error=NULL, dest_rel=?
               WHERE id=?""", (digest, warning, datetime.now().isoformat(timespec="seconds"), dest_rel, fid))

    # ---------------------------------------------------------------- reports
    def write_reports(self):
        L, R = self.ledger, self.reports
        self._lib_cache = (0.0, {})
        s = self.snapshot()
        now = datetime.now()

        def gb(n):
            return f"{(n or 0) / 1024 ** 3:,.1f} GB"

        lines = [
            "iPhone Photo Extractor - Export Report",
            f"Written {now:%Y-%m-%d %H:%M}   |   Phone: {self.device or 'unknown'}",
            f"Backup folder: {self.dest}",
            "=" * 72,
        ]
        if self.library_known:
            lines += [
                f"Items in your Photos library:               {s['library_items']:,}",
                f"  on the phone (copyable over USB):         {s['library_on_phone']:,}",
                f"  backed up and verified:                   {s['library_backed_up']:,}",
                f"  NOT on the phone (iCloud only):           {s['not_on_phone']:,}"
                + ("   -> see 'Not on phone (iCloud only).csv'" if s["not_on_phone"] else ""),
                "",
            ]
        lines += [
            f"Files found on the phone:                   {s['total_files']:,}  ({gb(s['total_bytes'])})",
            "  (includes Live Photo video halves and edit files, which are not separate library items)",
            f"  copied and verified:                      {s['done_files'] - s['duplicates']:,}",
            f"  identical copies skipped (stored once):   {s['duplicates']:,}",
            f"  still to copy:                            {s['pending']:,}",
            f"  failed after {MAX_ATTEMPTS} tries:                      {s['failed']:,}"
            + ("   -> see 'Failed files.csv' (use Retry failed)" if s["failed"] else ""),
            f"  copied, but damaged on the phone itself:  {s['warnings']:,}"
            + ("   -> see 'Files with warnings.csv'" if s["warnings"] else ""),
            "",
        ]
        complete = s["pending"] == 0 and s["failed"] == 0
        if self.library_known:
            all_on_phone_done = s["library_backed_up"] >= s["library_on_phone"] and complete
            lines.append("RESULT: every item on the phone is backed up and verified." if all_on_phone_done
                         else "RESULT: not finished yet - see the counts above.")
            if s["not_on_phone"]:
                lines.append(f"        {s['not_on_phone']:,} items exist only in iCloud and still need the iCloud step.")
        else:
            lines.append("RESULT: " + ("all files on the phone copied and verified." if complete
                                       else "not finished yet - see the counts above."))
        (R / "Export Report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        def dump(name, header, sql):
            rows = L.q(sql).fetchall()
            target = R / name
            if not rows:
                target.unlink(missing_ok=True)
                return
            with open(target, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)

        dump("Failed files.csv", ["phone_path", "planned_backup_name", "size_bytes", "attempts", "last_error"],
             "SELECT phone_path, dest_rel, size, attempts, last_error FROM files WHERE status='failed' ORDER BY id")
        dump("Files with warnings.csv", ["backup_file", "phone_path", "warning"],
             "SELECT dest_rel, phone_path, warning FROM files WHERE warning IS NOT NULL AND status='done' ORDER BY id")
        if self.library_known:
            dump("Not on phone (iCloud only).csv", ["original_name", "date_taken", "type", "hidden", "library_id"],
                 """SELECT orig_name, datetime(created,'unixepoch','localtime'),
                           CASE kind WHEN 1 THEN 'video' ELSE 'photo' END, hidden, uuid
                    FROM assets WHERE in_library=1 AND trashed=0 AND on_phone=0 ORDER BY created""")
        log.info("reports written")

    # ---------------------------------------------------------------- main loop
    async def run(self):
        from pymobiledevice3.services.afc import AfcService
        hb = asyncio.create_task(self.heartbeat())
        indexed = False
        backoff = 3
        try:
            while True:
                lockdown = await self.connect()
                self.say("connected", f"Connected to {self.device}")
                try:
                    async with AfcService(lockdown) as afc:
                        phone = ReadOnlyPhone(afc)
                        if not indexed:
                            await self.index(phone)
                            indexed = True
                            self.check_space()
                        backoff = 3
                        await self.transfer_all(phone)
                    self.current = None
                    self.write_reports()
                    s = self.snapshot()
                    self.say("complete", "Finished. " + (
                        f"{s['failed']:,} files failed; press Retry failed." if s["failed"] else
                        "Everything on the phone is backed up and verified."))
                    emit(self.snapshot())
                    return EXIT_COMPLETE
                except (DiskFull, FatalError):
                    raise
                except Exception as e:
                    log.warning("connection lost: %s\n%s", e, traceback.format_exc(limit=4))
                    self.say("reconnecting", f"Connection lost ({type(e).__name__}). Reconnecting in {backoff}s; "
                                             f"finished files are safe.")
                finally:
                    try:
                        await lockdown.close()
                    except Exception:
                        pass
                self.current = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            hb.cancel()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args()
    try:
        from pymobiledevice3.utils import get_asyncio_loop
    except ImportError:
        emit({"t": "fatal", "msg": "pymobiledevice3 is not installed. Start with 'Run Photo Extractor.bat'."})
        sys.exit(EXIT_FATAL)
    try:
        engine = Engine(args.dest, retry_failed=args.retry_failed)
    except FatalError as e:
        emit({"t": "fatal", "msg": str(e)})
        sys.exit(EXIT_FATAL)
    except OSError as e:
        emit({"t": "fatal", "msg": f"Cannot use the backup folder: {e}"})
        sys.exit(EXIT_FATAL)
    try:
        code = get_asyncio_loop().run_until_complete(engine.run())
    except DiskFull as e:
        log.error("%s", e)
        try:
            engine.write_reports()
        except Exception:
            pass
        emit({"t": "fatal", "msg": str(e)})
        code = EXIT_DISK_FULL
    except FatalError as e:
        emit({"t": "fatal", "msg": str(e)})
        code = EXIT_FATAL
    except Exception as e:
        log.error("engine crashed: %s\n%s", e, traceback.format_exc())
        emit({"t": "crash", "msg": f"{type(e).__name__}: {e}"})
        code = EXIT_RESTART
    sys.exit(code)


if __name__ == "__main__":
    main()
