#!/usr/bin/env python3
"""
iPhone Photo Library Probe  (read-only)

Checks, on YOUR iPhone and YOUR PC, everything the full extraction tool depends on:
  1. The Apple USB driver/service is installed and reachable
  2. The iPhone is found over USB and trusts this computer
  3. A read-only file connection (AFC) to the phone's media folder opens
  4. How many photos/videos are on the phone, and how big they are
  5. Whether the phone's photo database (Photos.sqlite) is readable, and from it:
       - whether iCloud Photos is in use
       - how many photos/videos exist ONLY in iCloud (not on the phone)
       - whether the files on the phone are full originals or reduced copies
  6. Real-world transfer speed and freeze behavior on one large video

SAFETY: The phone is only ever listed, inspected, and read. This program contains no code
that writes, renames, or deletes anything on the phone (see the ReadOnlyPhone class).
Nothing is copied to the PC except a private copy of the photo database used for counting,
which stays in the probe_output folder next to this script.
"""

import asyncio
import hashlib
import json
import platform
import shutil
import socket
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROBE_VERSION = "1.1"
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "probe_output"

OP_TIMEOUT = 30                     # seconds any single phone operation may take before it counts as a stall
STAT_CONCURRENCY = 8                # parallel file-info requests while counting
CHUNK = 1024 * 1024                 # 1 MB reads
SPEED_TEST_MAX_BYTES = 300 * 1024 ** 2
SPEED_TEST_MAX_SECONDS = 45
PAIR_WAIT_SECONDS = 180
APPLE_MUX_PORT = 27015              # Apple Mobile Device Service on Windows
CORE_DATA_EPOCH = datetime(2001, 1, 1)

PHOTO_EXT = {"HEIC", "HEIF", "JPG", "JPEG", "PNG", "DNG", "GIF", "TIF", "TIFF", "WEBP", "BMP"}
VIDEO_EXT = {"MOV", "MP4", "M4V", "HEVC", "3GP", "AVI"}
SIDECAR_EXT = {"AAE"}


class PhoneStall(Exception):
    """A single phone operation produced no answer within OP_TIMEOUT."""


class ReadOnlyPhone:
    """
    The ONLY object in this program that talks to the phone.
    It exposes exactly five operations: list a folder, get file info, open a file for
    READING, read bytes, close the file. There is no write, rename, or delete here, and the
    open mode is hard-coded to read-only.
    """

    def __init__(self, afc):
        self._afc = afc
        self.stalls = 0

    async def _guard(self, coro, what):
        try:
            return await asyncio.wait_for(coro, OP_TIMEOUT)
        except asyncio.TimeoutError:
            self.stalls += 1
            raise PhoneStall(f"no response for {OP_TIMEOUT}s during {what}")

    async def listdir(self, path):
        return await self._guard(self._afc.listdir(path), f"listdir {path}")

    async def stat(self, path):
        return await self._guard(self._afc.stat(path), f"stat {path}")

    async def open_read(self, path):
        return await self._guard(self._afc.fopen(path, "r"), f"open {path}")

    async def read(self, handle, size):
        return await self._guard(self._afc.fread(handle, size), "read")

    async def close(self, handle):
        try:
            await self._guard(self._afc.fclose(handle), "close")
        except Exception:
            pass


def human_bytes(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def human_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def ext_of(name):
    return name.rsplit(".", 1)[-1].upper() if "." in name else ""


def status_line(text):
    sys.stdout.write("\r" + text[:110].ljust(110))
    sys.stdout.flush()


class Report:
    def __init__(self):
        self.data = {
            "probe_version": PROBE_VERSION,
            "run_at": datetime.now().isoformat(timespec="seconds"),
            "pc": {"os": platform.platform(), "python": platform.python_version()},
            "steps": {},
        }
        self.lines = []

    def step(self, key, ok, summary, **details):
        self.data["steps"][key] = {"ok": ok, "summary": summary, **details}
        mark = "OK  " if ok else ("WARN" if ok is None else "FAIL")
        print(f"\n  [{mark}] {summary}")

    def save(self):
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "probe_report.json").write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        (OUT_DIR / "probe_report.txt").write_text(self.render_text(), encoding="utf-8")

    def render_text(self):
        d = self.data
        s = d["steps"]
        out = []
        out.append("=" * 70)
        out.append(f"iPhone Photo Library Probe v{PROBE_VERSION}   {d['run_at']}")
        out.append(f"PC: {d['pc']['os']}  |  Python {d['pc']['python']}")
        if "device" in d:
            dv = d["device"]
            out.append(f"iPhone: {dv.get('model')}  |  iOS {dv.get('ios')}")
        out.append("=" * 70)
        for key, st in s.items():
            mark = "OK  " if st["ok"] else ("WARN" if st["ok"] is None else "FAIL")
            out.append(f"[{mark}] {key}: {st['summary']}")
            for k, v in st.items():
                if k in ("ok", "summary"):
                    continue
                if isinstance(v, (dict, list)):
                    out.append(f"        {k}: {json.dumps(v, default=str)}")
                else:
                    out.append(f"        {k}: {v}")
        if "verdict" in d:
            out.append("-" * 70)
            out.append("VERDICT")
            for v in d["verdict"]:
                out.append(f"  * {v}")
        out.append("=" * 70)
        return "\n".join(out)


# ----------------------------------------------------------------------------- step 1
def check_apple_service(report):
    if sys.platform != "win32":
        report.step("apple_driver", None, "Not on Windows; skipping Apple service check")
        return True
    try:
        with socket.create_connection(("127.0.0.1", APPLE_MUX_PORT), timeout=3):
            pass
        report.step("apple_driver", True, "Apple Mobile Device Service is running")
        return True
    except OSError:
        report.step(
            "apple_driver", False,
            "Apple Mobile Device Service not reachable",
            fix="Install 'Apple Devices' from the Microsoft Store (or iTunes), open it once with the "
                "iPhone plugged in, then run this probe again.",
        )
        return False


# ----------------------------------------------------------------------------- step 2
async def connect_phone(report):
    from pymobiledevice3 import usbmux
    from pymobiledevice3.exceptions import (
        PairingDialogResponsePendingError, PasswordRequiredError, UserDeniedPairingError, NotPairedError,
    )
    from pymobiledevice3.lockdown import create_using_usbmux

    deadline = time.monotonic() + PAIR_WAIT_SECONDS
    last_msg = None
    while True:
        try:
            devices = await usbmux.list_devices()
        except Exception as e:
            report.step("find_iphone", False, f"Could not talk to the Apple USB service: {e}")
            return None
        usb = [d for d in devices if d.is_usb]
        if not usb:
            msg = "No iPhone found on USB. Plug it in (rear USB port) and unlock it."
            if any(d.is_network for d in devices):
                msg += " (A phone is visible over Wi-Fi only; this probe uses USB.)"
        else:
            if len(usb) > 1:
                print(f"\n  Note: {len(usb)} Apple devices on USB; using the first one.")
            try:
                lockdown = await create_using_usbmux(serial=usb[0].serial, connection_type="USB", pair_timeout=60)
                info = lockdown.all_values
                report.data["device"] = {
                    "model": info.get("ProductType"),
                    "ios": info.get("ProductVersion"),
                    "build": info.get("BuildVersion"),
                }
                report.step("find_iphone", True,
                            f"Connected to {info.get('ProductType')} on iOS {info.get('ProductVersion')}")
                return lockdown
            except PasswordRequiredError:
                msg = "iPhone is locked. Unlock it and keep it on the home screen."
            except PairingDialogResponsePendingError:
                msg = "Tap 'Trust' on the iPhone and enter your passcode."
            except UserDeniedPairingError:
                report.step("find_iphone", False,
                            "'Don't Trust' was tapped on the iPhone",
                            fix="Unplug, plug back in, and tap 'Trust' this time.")
                return None
            except NotPairedError:
                msg = "Waiting for the iPhone to trust this computer (unlock it and tap 'Trust')."
            except Exception as e:
                msg = f"Connection attempt failed ({type(e).__name__}: {e}); retrying."
        if time.monotonic() > deadline:
            report.step("find_iphone", False, f"Gave up after {PAIR_WAIT_SECONDS}s: {msg}")
            return None
        if msg != last_msg:
            print(f"\n  ... {msg}")
            last_msg = msg
        await asyncio.sleep(3)


# ----------------------------------------------------------------------------- step 4
async def scan_dcim(phone, report):
    """Walk /DCIM one folder at a time. Returns {UPPERCASE_RELPATH: size}."""
    files = {}
    by_ext = Counter()
    bytes_by_kind = Counter()
    count_by_kind = Counter()
    errors = []
    largest_video = None
    start = time.monotonic()

    try:
        top = sorted(await phone.listdir("/DCIM"))
    except Exception as e:
        report.step("count_media", False, f"Could not list /DCIM: {type(e).__name__}: {e}")
        return None, None

    sem = asyncio.Semaphore(STAT_CONCURRENCY)

    async def info(path):
        async with sem:
            return await phone.stat(path)

    folders = []
    for name in top:
        try:
            st = await phone.stat(f"/DCIM/{name}")
            if st.get("st_ifmt") == "S_IFDIR":
                folders.append(name)
        except Exception as e:
            errors.append(f"/DCIM/{name}: {type(e).__name__}")

    for fi, folder in enumerate(folders, 1):
        folder_path = f"/DCIM/{folder}"
        try:
            names = await phone.listdir(folder_path)
        except Exception as e:
            errors.append(f"{folder_path}: {type(e).__name__}: {e}")
            continue
        results = await asyncio.gather(*(info(f"{folder_path}/{n}") for n in names), return_exceptions=True)
        for n, st in zip(names, results):
            if isinstance(st, Exception):
                errors.append(f"{folder_path}/{n}: {type(st).__name__}")
                continue
            if st.get("st_ifmt") != "S_IFREG":
                continue
            size = st.get("st_size", 0)
            rel = f"DCIM/{folder}/{n}".upper()
            files[rel] = size
            e = ext_of(n)
            by_ext[e] += 1
            kind = "photo" if e in PHOTO_EXT else "video" if e in VIDEO_EXT else "sidecar" if e in SIDECAR_EXT else "other"
            count_by_kind[kind] += 1
            bytes_by_kind[kind] += size
            if kind == "video" and (largest_video is None or size > largest_video[1]):
                largest_video = (f"{folder_path}/{n}", size)
        elapsed = time.monotonic() - start
        status_line(f"  Counting: folder {fi}/{len(folders)}  |  {len(files):,} files  |  "
                    f"{human_bytes(sum(bytes_by_kind.values()))}  |  {human_duration(elapsed)}")
        if phone.stalls >= 5:
            errors.append("Stopped counting early: 5 stalls")
            break

    elapsed = time.monotonic() - start
    total_bytes = sum(bytes_by_kind.values())
    ok = True if not errors else None
    report.step(
        "count_media", ok,
        f"{count_by_kind['photo']:,} photos + {count_by_kind['video']:,} videos on the phone, "
        f"{human_bytes(total_bytes)} total (counted in {human_duration(elapsed)})",
        dcim_folders=len(folders),
        photos=count_by_kind["photo"], videos=count_by_kind["video"],
        sidecars_aae=count_by_kind["sidecar"], other_files=count_by_kind["other"],
        photo_bytes=human_bytes(bytes_by_kind["photo"]), video_bytes=human_bytes(bytes_by_kind["video"]),
        total_bytes=total_bytes, total_human=human_bytes(total_bytes),
        by_extension=dict(by_ext.most_common()),
        stalls=phone.stalls, error_count=len(errors), first_errors=errors[:10],
    )
    return files, largest_video


# ----------------------------------------------------------------------------- step 5
async def copy_remote(phone, remote, local):
    h = await phone.open_read(remote)
    written = 0
    try:
        with open(local, "wb") as f:
            while True:
                data = await phone.read(h, CHUNK)
                if not data:
                    break
                f.write(data)
                written += len(data)
    finally:
        await phone.close(h)
    return written


async def copy_photos_db(phone, report):
    from pymobiledevice3.exceptions import AfcFileNotFoundError
    dest = OUT_DIR / "photos_db"
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("Photos.sqlite*"):
        old.unlink()
    copied = {}
    for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
        remote = f"/PhotoData/{name}"
        try:
            await phone.stat(remote)
        except AfcFileNotFoundError:
            if name == "Photos.sqlite":
                report.step("photo_database", False, "Photos.sqlite not found at /PhotoData on this iOS version")
                return None
            continue
        except Exception as e:
            report.step("photo_database", False, f"Photos.sqlite not accessible: {type(e).__name__}: {e}")
            return None
        try:
            status_line(f"  Copying {name} (read-only) ...")
            copied[name] = await copy_remote(phone, remote, dest / name)
        except Exception as e:
            if name == "Photos.sqlite":
                report.step("photo_database", False, f"Could not read Photos.sqlite: {type(e).__name__}: {e}")
                return None
    print()
    return dest / "Photos.sqlite", copied


def analyze_db(db_path, files, report):
    try:
        con = sqlite3.connect(str(db_path))
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except Exception as e:
        report.step("photo_database", False, f"Copied database could not be opened: {e}")
        return None

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    asset_t = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET" if "ZGENERICASSET" in tables else None
    if not asset_t:
        report.step("photo_database", False, "Database opened, but no asset table found (unexpected format)")
        return None

    def cols(t):
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}

    ac = cols(asset_t)
    attr_t = "ZADDITIONALASSETATTRIBUTES" if "ZADDITIONALASSETATTRIBUTES" in tables else None
    tc = cols(attr_t) if attr_t else set()

    def a(col, alias):
        return f"a.{col} AS {alias}" if col in ac else f"NULL AS {alias}"

    select = [
        "a.Z_PK AS pk",
        a("ZDIRECTORY", "dir"), a("ZFILENAME", "fname"), a("ZKIND", "kind"),
        a("ZTRASHEDSTATE", "trashed"), a("ZHIDDEN", "hidden"),
        a("ZCLOUDASSETGUID", "cloudguid"), a("ZDATECREATED", "created"),
    ]
    join = ""
    if attr_t and "ZASSET" in tc and "ZORIGINALFILESIZE" in tc:
        select.append("t.ZORIGINALFILESIZE AS origsize")
        join = f"LEFT JOIN {attr_t} t ON t.ZASSET = a.Z_PK"
    else:
        select.append("NULL AS origsize")
    sql = f"SELECT {', '.join(select)} FROM {asset_t} a {join}"

    c = Counter()
    other_dirs = Counter()
    outside = []          # (dir, fname, kind_name, origsize) for items stored outside DCIM
    missing_bytes = 0
    referenced = set()
    oldest = newest = None

    for pk, d, fname, kind, trashed, hidden, cloudguid, created, origsize in con.execute(sql):
        kind_name = "video" if kind == 1 else "photo"
        if trashed == 1:
            c["trashed"] += 1
            if d and fname:
                referenced.add(f"{d}/{fname}".upper())
            continue
        c["assets"] += 1
        c[f"assets_{kind_name}"] += 1
        if hidden == 1:
            c["hidden"] += 1
        if cloudguid:
            c["in_icloud"] += 1
        if created is not None:
            try:
                dt = CORE_DATA_EPOCH + timedelta(seconds=float(created))
                oldest = dt if oldest is None or dt < oldest else oldest
                newest = dt if newest is None or dt > newest else newest
            except Exception:
                pass
        if not d or not fname:
            c["no_path"] += 1
            continue
        path = f"{d}/{fname}".upper()
        referenced.add(path)
        if not path.startswith("DCIM/"):
            other_dirs["/".join(d.split("/")[:2])] += 1
            outside.append((d, fname, kind_name, origsize))
            continue
        size = files.get(path)
        if size is None:
            c["only_in_icloud"] += 1
            c[f"only_in_icloud_{kind_name}"] += 1
            if origsize:
                missing_bytes += origsize
        else:
            c["on_phone"] += 1
            if origsize:
                if size == origsize:
                    c["full_original"] += 1
                elif size < origsize:
                    c["reduced_copy"] += 1
                else:
                    c["size_differs"] += 1
            else:
                c["original_size_unknown"] += 1
    con.close()

    unreferenced = Counter(ext_of(p) for p in files if p not in referenced)

    ok = True
    summary = (f"Readable. {c['assets']:,} items in your library; {c['on_phone']:,} in the camera folder, "
               f"{len(outside):,} stored elsewhere (checked in the next step)")
    report.step(
        "photo_database", ok, summary,
        library_items=c["assets"], library_photos=c["assets_photo"], library_videos=c["assets_video"],
        on_phone=c["on_phone"],
        on_phone_full_original=c["full_original"], on_phone_reduced_copy=c["reduced_copy"],
        on_phone_original_size_unknown=c["original_size_unknown"], on_phone_size_differs=c["size_differs"],
        only_in_icloud=c["only_in_icloud"], only_in_icloud_photos=c["only_in_icloud_photo"],
        only_in_icloud_videos=c["only_in_icloud_video"], only_in_icloud_est_size=human_bytes(missing_bytes),
        synced_with_icloud=c["in_icloud"], hidden=c["hidden"], recently_deleted=c["trashed"],
        stored_outside_dcim_total=len(outside),
        stored_outside_dcim=dict(other_dirs.most_common(15)), items_without_path=c["no_path"],
        phone_files_not_in_library_by_ext=dict(unreferenced.most_common(12)),
        oldest=oldest.date().isoformat() if oldest else None,
        newest=newest.date().isoformat() if newest else None,
    )
    return c, outside


# ----------------------------------------------------------------------------- step 5b
async def check_outside_dcim(phone, outside, report):
    """Items the library stores outside DCIM (e.g. originals synced down from iCloud). Check each one."""
    from pymobiledevice3.exceptions import AfcFileNotFoundError
    c = Counter()
    by_group = defaultdict(Counter)
    err_types = Counter()
    missing_bytes = 0
    present_bytes = 0
    sem = asyncio.Semaphore(STAT_CONCURRENCY)

    async def check(item):
        d, fname, kind_name, origsize = item
        async with sem:
            try:
                st = await phone.stat(f"/{d}/{fname}")
                return item, st, None
            except AfcFileNotFoundError:
                return item, None, "missing"
            except Exception as e:
                return item, None, type(e).__name__

    start = time.monotonic()
    batch = 500
    for i in range(0, len(outside), batch):
        for (d, fname, kind_name, origsize), st, err in await asyncio.gather(*(check(x) for x in outside[i:i + batch])):
            group = "/".join(d.split("/")[:2])
            if err:
                key = "missing" if err == "missing" else "unreadable"
                if key == "unreadable":
                    err_types[err] += 1
                if origsize:
                    missing_bytes += origsize
            else:
                size = st.get("st_size", 0)
                present_bytes += size
                if not origsize:
                    key = "present_size_unknown"
                elif size == origsize:
                    key = "present_full_original"
                elif size < origsize:
                    key = "present_reduced_copy"
                    missing_bytes += origsize
                else:
                    key = "present_size_differs"
            c[key] += 1
            c[f"{key}_{kind_name}"] += 1
            by_group[group][key] += 1
        status_line(f"  Checking items outside the camera folder: {min(i + batch, len(outside)):,}/{len(outside):,}")
        if phone.stalls >= 5:
            err_types["stopped early: 5 stalls"] += 1
            break
    print()

    not_full = c["missing"] + c["unreadable"] + c["present_reduced_copy"]
    report.step(
        "outside_camera_folder", True if not err_types else None,
        f"{len(outside):,} items stored outside DCIM: {c['present_full_original']:,} full originals on the phone, "
        f"{not_full:,} NOT available at full quality over USB (checked in {human_duration(time.monotonic() - start)})",
        total=len(outside),
        present_full_original=c["present_full_original"], present_reduced_copy=c["present_reduced_copy"],
        present_size_unknown=c["present_size_unknown"], present_size_differs=c["present_size_differs"],
        missing_from_phone=c["missing"], unreadable=c["unreadable"],
        not_full_photos=c["missing_photo"] + c["unreadable_photo"] + c["present_reduced_copy_photo"],
        not_full_videos=c["missing_video"] + c["unreadable_video"] + c["present_reduced_copy_video"],
        full_originals_size=human_bytes(present_bytes),
        not_full_est_size=human_bytes(missing_bytes), not_full_est_bytes=missing_bytes,
        by_location={g: dict(v) for g, v in sorted(by_group.items(), key=lambda kv: -sum(kv[1].values()))[:12]},
        unreadable_error_types=dict(err_types),
    )
    return c, missing_bytes, present_bytes


def drive_space():
    """Free space on every local drive (Windows) or on this folder's drive (elsewhere)."""
    out = {}
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                u = shutil.disk_usage(root)
                out[f"{letter}:"] = u.free
            except OSError:
                continue
    else:
        out[str(HERE.anchor or "/")] = shutil.disk_usage(HERE).free
    return out


# ----------------------------------------------------------------------------- step 6
async def speed_test(phone, target, report):
    if not target:
        report.step("speed_test", None, "No video found to test with; skipped")
        return None
    path, size = target
    print(f"\n  Reading {human_bytes(size)} video for up to {SPEED_TEST_MAX_SECONDS}s (nothing is saved)...")
    hasher = hashlib.sha256()
    read = 0
    slowest = 0.0
    stalls_before = phone.stalls
    start = time.monotonic()
    try:
        h = await phone.open_read(path)
    except Exception as e:
        report.step("speed_test", False, f"Could not open test file: {type(e).__name__}: {e}")
        return None
    try:
        while read < SPEED_TEST_MAX_BYTES and time.monotonic() - start < SPEED_TEST_MAX_SECONDS:
            t0 = time.monotonic()
            data = await phone.read(h, CHUNK)
            dt = time.monotonic() - t0
            if not data:
                break
            slowest = max(slowest, dt)
            hasher.update(data)
            read += len(data)
            el = time.monotonic() - start
            status_line(f"  Speed test: {human_bytes(read)}  |  {read / el / 1024**2:,.1f} MB/s")
    except Exception as e:
        report.step("speed_test", False, f"Read failed after {human_bytes(read)}: {type(e).__name__}: {e}")
        return None
    finally:
        await phone.close(h)
    el = time.monotonic() - start
    mbps = read / el / 1024 ** 2 if el else 0
    report.step(
        "speed_test", True, f"{mbps:,.1f} MB/s sustained over {human_bytes(read)}",
        mb_per_sec=round(mbps, 2), bytes_read=read, seconds=round(el, 1),
        slowest_1mb_chunk_sec=round(slowest, 3), stalls=phone.stalls - stalls_before,
        whole_file_read=(read == size),
    )
    return mbps


# ----------------------------------------------------------------------------- verdict
def verdict(report, files_total_bytes, db, outside, mbps):
    v = []
    s = report.data["steps"]
    if s.get("open_media", {}).get("ok"):
        v.append("Direct read-only USB access works on this phone: the full tool can be built on it.")

    usb_bytes = files_total_bytes
    if db:
        oc, out_missing_bytes, out_present_bytes = outside if outside else (Counter(), 0, 0)
        usb_bytes += out_present_bytes
        cloud_only = db["only_in_icloud"] + oc["missing"] + oc["unreadable"]
        reduced = db["reduced_copy"] + oc["present_reduced_copy"]
        not_full = cloud_only + reduced
        if not_full:
            v.append(f"{not_full:,} library items are NOT on the phone at full quality "
                     f"({cloud_only:,} only in iCloud, {reduced:,} reduced copies). "
                     f"Those can only come from iCloud (icloudpd step). Approx size: "
                     f"{human_bytes(s.get('outside_camera_folder', {}).get('not_full_est_bytes', 0))}+.")
        elif db["in_icloud"] > 0:
            v.append("iCloud Photos is ON, and every item's full original was found on the phone. "
                     "USB alone can get the whole library.")
        else:
            v.append("iCloud Photos appears to be OFF. Everything is on the phone; USB gets it all.")
        if db["trashed"]:
            v.append(f"{db['trashed']:,} items are in 'Recently Deleted' (the tool will skip them by default).")

    if usb_bytes:
        drives = drive_space()
        need = usb_bytes * 1.05
        fits = [f"{d} ({human_bytes(f)} free)" for d, f in drives.items() if f > need]
        allfree = ", ".join(f"{d} {human_bytes(f)}" for d, f in drives.items())
        v.append(f"Reachable over USB: {human_bytes(usb_bytes)}. Free space by drive: {allfree}.")
        v.append("Drives with enough room: " + (", ".join(fits) if fits else
                 "NONE -> connect an external drive with at least " + human_bytes(need) + " free."))
        if mbps:
            v.append(f"Estimated full USB transfer time at the measured speed: "
                     f"~{human_duration(usb_bytes / (mbps * 1024 ** 2))}.")
    report.data["verdict"] = v


# ----------------------------------------------------------------------------- main
async def run(report):
    print("\n[1/6] Checking the Apple USB driver")
    if not check_apple_service(report):
        return

    print("\n[2/6] Looking for the iPhone (unlock it; tap 'Trust' if asked)")
    lockdown = await connect_phone(report)
    if not lockdown:
        return

    from pymobiledevice3.services.afc import AfcService

    files, largest_video, db, outside, mbps = None, None, None, None, None
    try:
        print("\n[3/6] Opening a read-only connection to the photo storage")
        async with AfcService(lockdown) as afc:
            phone = ReadOnlyPhone(afc)
            try:
                top = await phone.listdir("/")
                report.step("open_media", True, "Read-only file connection opened",
                            media_root_has_DCIM="DCIM" in top, media_root_has_PhotoData="PhotoData" in top)
            except Exception as e:
                report.step("open_media", False, f"Could not open the file connection: {type(e).__name__}: {e}")
                return

            print("\n[4/6] Counting photos and videos (large libraries take a few minutes)")
            files, largest_video = await scan_dcim(phone, report)

            print("\n[5/6] Reading the photo library database")
            got = await copy_photos_db(phone, report)
            if got and files is not None:
                res = analyze_db(got[0], files, report)
                if res:
                    db, outside_items = res
                    if outside_items:
                        outside = await check_outside_dcim(phone, outside_items, report)

            print("\n[6/6] Measuring transfer speed")
            mbps = await speed_test(phone, largest_video, report)
    except Exception as e:
        report.step("connection", False, f"Connection lost: {type(e).__name__}: {e}",
                    detail=traceback.format_exc(limit=3))
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass

    verdict(report, sum(files.values()) if files else 0, db, outside, mbps)


def main():
    print("=" * 70)
    print(f" iPhone Photo Library Probe v{PROBE_VERSION}  -  READ-ONLY, changes nothing on the phone")
    print("=" * 70)
    try:
        import pymobiledevice3  # noqa: F401
        from pymobiledevice3.utils import get_asyncio_loop
    except ImportError:
        print("\npymobiledevice3 is not installed. Run 'Run Probe.bat' instead of this file directly.")
        sys.exit(1)

    report = Report()
    try:
        get_asyncio_loop().run_until_complete(run(report))
    except KeyboardInterrupt:
        report.step("cancelled", None, "Stopped by user (Ctrl+C)")
    except Exception as e:
        report.step("crash", False, f"Unexpected error: {type(e).__name__}: {e}",
                    detail=traceback.format_exc(limit=5))
    report.save()
    print("\n\n" + report.render_text())
    print(f"\nReport saved to: {OUT_DIR / 'probe_report.txt'}")
    print("Keep this report; it shows whether the extractor will work with your phone.")


if __name__ == "__main__":
    main()
