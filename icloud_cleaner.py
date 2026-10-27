#!/usr/bin/env python3
"""
iCloud Photo Cleaner

Two jobs, both limited to photos the iPhone Photo Extractor has already backed up and verified:
  1. Delete them from your iCloud Photos library. They go to "Recently Deleted" (30-day undo), exactly
     like deleting on icloud.com.
  2. PERMANENTLY remove them from "Recently Deleted" (marks them expunged, the same change used by
     icloud.com / icloudpd to empty that album). This cannot be undone.

How it avoids getting the account locked:
  * Signs in once. The trusted session is saved on this PC and reused on later runs, so Apple sees
    one sign-in, not many. A rejected password is never retried automatically.
  * Paced like a person: 200 deletions per request, a pause between requests, a longer break after
    every 1,000. If Apple asks it to slow down, it waits; if Apple rejects the session, it stops.

How it protects your photos:
  * Every iCloud photo is matched to a backed-up file (original name + size, or name + date taken),
    and the backed-up file must exist on disk. Anything unmatched is skipped and listed, never deleted.
  * It deletes one photo first and waits for you to confirm before doing the rest.
  * Progress is saved; stopping (Ctrl+C) and running again continues where it left off.
"""

import csv
import getpass
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1"
HERE = Path(__file__).resolve().parent
EXTRACTOR_SETTINGS = HERE / "extractor_settings.json"
CLEANER_SETTINGS = HERE / "icloud_cleaner_settings.json"
SESSION_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "iPhonePhotoExtractor" / "icloud_session"

BATCH = 200                 # deletions per request
PAUSE_BETWEEN = 3           # seconds between requests
PAUSE_PER_1000 = 15         # extra seconds after every 1,000 deletions
SCAN_PAUSE_EVERY = 100      # scanning: pause after this many items...
SCAN_PAUSE = 0.3            # ...for this many seconds
MAX_TRANSIENT_RETRIES = 4
DATE_TOLERANCE = 2          # seconds, for name + date matching


MODES = {
    "library": {
        "title": "Delete backed-up photos from your iCloud library (they go to Recently Deleted, 30-day undo)",
        "album": lambda photos: photos.all,
        "field": "isDeleted",
        "db": "icloud_cleaner.db",
        "report": "iCloud Cleaner Report.txt",
        "unmatched": "iCloud - not deleted (no matching backup).csv",
        "where": "your iCloud photo list",
        "done": "Deleted from iCloud (now in Recently Deleted, 30-day undo)",
        "check": "is now in Recently Deleted on icloud.com",
    },
    "trash": {
        "title": "PERMANENTLY empty Recently Deleted (backed-up photos only - cannot be undone)",
        "album": lambda photos: photos.albums["Recently Deleted"],
        "field": "isExpunged",
        "db": "icloud_cleaner_trash.db",
        "report": "iCloud Recently Deleted Report.txt",
        "unmatched": "iCloud Recently Deleted - kept (no matching backup).csv",
        "where": "iCloud's Recently Deleted album",
        "done": "PERMANENTLY deleted from Recently Deleted",
        "check": "is GONE from Recently Deleted on icloud.com (refresh the page)",
    },
}


class Stop(Exception):
    """Stop cleanly with a message for the user."""


def say(msg=""):
    print(msg, flush=True)


def line(msg):
    sys.stdout.write("\r" + msg[:118].ljust(118))
    sys.stdout.flush()


def ask(prompt, default=None):
    full = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        ans = input(full).strip()
    except EOFError:
        ans = ""
    return ans or (default or "")


def load_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(p, data):
    try:
        Path(p).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def human_duration(seconds):
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# ============================================================================ backup index
class BackupIndex:
    """Everything the extractor verified, keyed for matching against iCloud."""

    def __init__(self, dest):
        self.dest = Path(dest)
        ledger = self.dest / ".extractor" / "transfer.db"
        if not ledger.exists():
            raise Stop(f"No backup ledger found at {ledger}.\n"
                       "Run the iPhone Photo Extractor first (or check the backup folder).")
        con = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            rows = con.execute("""
                SELECT a.orig_name, a.orig_size, a.created, f.dest_rel
                FROM assets a JOIN files f ON f.uuid = a.uuid AND f.role = 'main'
                WHERE f.status IN ('done', 'duplicate') AND a.trashed = 0 AND a.orig_name IS NOT NULL
            """).fetchall()
        finally:
            con.close()
        self.by_name_size = set()
        self.by_name_dates = {}
        self.count = 0
        missing = 0
        for name, size, created, rel in rows:
            if not rel or not (self.dest / rel).exists():
                missing += 1
                continue
            n = name.lower()
            self.count += 1
            if size:
                self.by_name_size.add((n, int(size)))
            if created is not None:
                self.by_name_dates.setdefault(n, []).append(float(created))
        self.missing_on_disk = missing

    def match(self, filename, size, taken_ts):
        n = (filename or "").lower()
        if not n:
            return None
        if size and (n, int(size)) in self.by_name_size:
            return "name+size"
        if taken_ts is not None:
            for t in self.by_name_dates.get(n, ()):
                if abs(t - taken_ts) <= DATE_TOLERANCE:
                    return "name+date"
        return None


# ============================================================================ local progress
class Progress:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS items (
                record_name TEXT PRIMARY KEY, record_type TEXT, change_tag TEXT, zone TEXT,
                filename TEXT, size INTEGER, taken REAL, matched TEXT,
                status TEXT, seen_scan INTEGER, deleted_at TEXT, last_error TEXT);
            CREATE INDEX IF NOT EXISTS items_status ON items(status, matched);
        """)

    def meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


# ============================================================================ iCloud helpers
def minimal_record(asset):
    """Pull just what a delete needs from a pyicloud PhotoAsset."""
    from pyicloud.services.photos_cloudkit.mappers import (
        record_change_tag, record_name, record_record_type, record_zone,
    )
    from pyicloud.services.photos_cloudkit.constants import PRIMARY_ZONE
    ar, mr = asset.asset_record, asset.master_record
    taken = asset.asset_date
    taken_ts = taken.timestamp() if taken and taken.year > 1970 else None
    return {
        "record_name": record_name(ar),
        "record_type": record_record_type(ar),
        "change_tag": record_change_tag(ar) or record_change_tag(mr),
        "zone": json.dumps(record_zone(ar) or PRIMARY_ZONE, sort_keys=True),
        "filename": asset.filename,
        "size": asset.size,
        "taken": taken_ts,
    }


def build_ops(items, field="isDeleted"):
    from pyicloud.common.cloudkit import CKModifyOperation, CKWriteRecord, CKZoneID
    ops = []
    for it in items:
        ops.append(CKModifyOperation(
            operationType="update",
            record=CKWriteRecord(
                recordName=it["record_name"],
                recordType=it["record_type"],
                recordChangeTag=it["change_tag"],
                fields={field: {"type": "INT64", "value": 1}},
                zoneID=CKZoneID(**json.loads(it["zone"])),
            ),
        ))
    return ops


def zone_req(zone_json):
    from pyicloud.common.cloudkit import CKZoneIDReq
    z = json.loads(zone_json)
    return CKZoneIDReq(zoneName=z["zoneName"], ownerRecordName=z.get("ownerRecordName"), zoneType=z.get("zoneType"))


def is_auth_problem(exc):
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("401", "403", "421", "authentication", "auth required", "unauthorized",
                                   "forbidden", "login", "session"))


# ============================================================================ cleaner
class Cleaner:
    def __init__(self, dest, apple_id, mode="library"):
        self.dest = Path(dest)
        self.apple_id = apple_id
        self.mode = mode
        self.m = MODES[mode]
        self.work = self.dest / ".extractor"
        self.reports = self.dest / "_Export Reports"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.progress = Progress(self.work / self.m["db"])
        self.api = None
        self.photos = None

    # ---------------------------------------------------------------- sign in
    def sign_in(self):
        from pyicloud import PyiCloudService
        from pyicloud.exceptions import PyiCloudAccountLockedException, PyiCloudFailedLoginException
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        say("\nSigning in to iCloud. Your password is used only for this sign-in and is never saved.")
        say("(If this PC was signed in before, the saved session is reused and Apple won't ask again.)")
        password = getpass.getpass("Apple Account password (typing is hidden): ")
        if not password:
            raise Stop("No password entered.")
        try:
            api = PyiCloudService(self.apple_id, password, cookie_directory=str(SESSION_DIR))
        except PyiCloudAccountLockedException:
            raise Stop("Apple says the account is LOCKED. Unlock it first (iPhone: Settings > your name > "
                       "Sign-In & Security > Change Password), then run this again.")
        except PyiCloudFailedLoginException as e:
            raise Stop(f"Apple did not accept the sign-in ({e}). Not retrying, to protect the account.\n"
                       "Check the password on icloud.com in a browser first, then run this again.")
        finally:
            password = None

        if api.requires_2fa:
            say("\nApple sent a sign-in request to your iPhone. Tap 'Allow', then type the 6-digit code here.")
            for attempt in range(3):
                code = ask("6-digit code").replace(" ", "")
                if code and api.validate_2fa_code(code):
                    break
                say("That code wasn't accepted." + (" Try again." if attempt < 2 else ""))
            else:
                raise Stop("Code not accepted 3 times. Stopping to protect the account; run again later.")
            if not api.is_trusted_session:
                api.trust_session()
            say("Signed in, and this PC is now trusted (no code needed next time).")
        elif getattr(api, "requires_2sa", False):
            raise Stop("This account uses an older verification method this tool doesn't support.")
        else:
            say("Signed in.")
        self.api = api
        self.photos = api.photos

    # ---------------------------------------------------------------- scan
    def scan(self, backup):
        say(f"\nStep 1 of 3: reading {self.m['where']} (nothing is changed)...")
        P = self.progress.db
        scan_no = int(self.progress.meta("scan_no", 0)) + 1
        library = self.m["album"](self.photos)
        try:
            total = len(library)
        except Exception:
            total = None
        start = time.monotonic()
        n = 0
        P.execute("BEGIN")
        try:
            for asset in library:
                rec = minimal_record(asset)
                matched = backup.match(rec["filename"], rec["size"], rec["taken"])
                P.execute("""INSERT INTO items(record_name, record_type, change_tag, zone, filename, size, taken,
                                               matched, status, seen_scan)
                             VALUES(?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(record_name) DO UPDATE SET record_type=excluded.record_type,
                               change_tag=excluded.change_tag, zone=excluded.zone, filename=excluded.filename,
                               size=excluded.size, taken=excluded.taken, matched=excluded.matched,
                               seen_scan=excluded.seen_scan, status=excluded.status""",
                          (rec["record_name"], rec["record_type"], rec["change_tag"], rec["zone"], rec["filename"],
                           rec["size"], rec["taken"], matched, "pending" if matched else "skipped", scan_no))
                n += 1
                if n % SCAN_PAUSE_EVERY == 0:
                    P.execute("COMMIT")
                    P.execute("BEGIN")
                    el = time.monotonic() - start
                    line(f"  Read {n:,}" + (f" of {total:,}" if total else "") + f" photos  ({human_duration(el)})")
                    time.sleep(SCAN_PAUSE)
            P.execute("COMMIT")
        except BaseException:
            P.execute("ROLLBACK")
            raise
        say("")
        # anything not seen this scan and not deleted by us is gone from iCloud already
        P.execute("UPDATE items SET status='gone' WHERE seen_scan IS NOT ? AND status IN ('pending','skipped')",
                  (scan_no,))
        self.progress.set_meta("scan_no", scan_no)
        return n

    def summary(self):
        P = self.progress.db
        c = dict(P.execute("SELECT status, COUNT(*) FROM items WHERE status IN ('pending','skipped','error') "
                           "GROUP BY status").fetchall())
        return c.get("pending", 0), c.get("skipped", 0), c.get("error", 0)

    def write_unmatched_report(self):
        rows = self.progress.db.execute("""SELECT filename, size, datetime(taken,'unixepoch','localtime'), record_name
                                           FROM items WHERE status='skipped' ORDER BY taken""").fetchall()
        path = self.reports / self.m["unmatched"]
        if not rows:
            path.unlink(missing_ok=True)
            return None
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["filename", "size_bytes", "date_taken", "icloud_id"])
            w.writerows(rows)
        return path

    # ---------------------------------------------------------------- delete
    def _delete_batch(self, items):
        """Delete one batch. Returns (ok_names, {name: error}). Raises Stop on auth trouble."""
        from pyicloud.common.cloudkit import CKErrorItem
        from pyicloud.common.cloudkit.client import CloudKitRateLimited
        by_zone = {}
        for it in items:
            by_zone.setdefault(it["zone"], []).append(it)
        ok, failed = [], {}
        for zone, group in by_zone.items():
            attempt = 0
            while True:
                try:
                    resp = self.photos.private_client.modify(operations=build_ops(group, self.m["field"]),
                                                             zone_id=zone_req(zone), atomic=False)
                    break
                except CloudKitRateLimited as e:
                    wait = max(float(getattr(e, "retry_after", None) or 0), 60 * (attempt + 1))
                    attempt += 1
                    if attempt > MAX_TRANSIENT_RETRIES:
                        raise Stop("Apple keeps asking to slow down. Stopping for now; run again in an hour "
                                   "and it will continue where it left off.")
                    say(f"\n  Apple asked to slow down. Waiting {int(wait)}s...")
                    time.sleep(wait)
                except Exception as e:
                    if is_auth_problem(e):
                        raise Stop("Apple ended the session. Stopping (not signing in again automatically).\n"
                                   "Run this again later; it continues where it left off.")
                    attempt += 1
                    if attempt > MAX_TRANSIENT_RETRIES:
                        raise Stop(f"Network/iCloud error kept happening ({type(e).__name__}: {e}). "
                                   "Run again later; it continues where it left off.")
                    wait = 20 * attempt
                    say(f"\n  Temporary problem ({type(e).__name__}); retrying in {wait}s...")
                    time.sleep(wait)
            names = {it["record_name"] for it in group}
            reported = set()
            for r in getattr(resp, "records", []) or []:
                rn = getattr(r, "recordName", None)
                if rn is None:
                    continue
                reported.add(rn)
                if isinstance(r, CKErrorItem):
                    failed[rn] = f"{r.serverErrorCode}: {r.reason or ''}".strip()
                else:
                    ok.append(rn)
            for rn in names - reported:
                failed[rn] = "no confirmation from iCloud"
        return ok, failed

    def _record(self, ok, failed):
        P = self.progress.db
        now = datetime.now().isoformat(timespec="seconds")
        P.execute("BEGIN")
        for rn in ok:
            P.execute("UPDATE items SET status='deleted', deleted_at=?, last_error=NULL WHERE record_name=?", (now, rn))
        for rn, err in failed.items():
            P.execute("UPDATE items SET status='error', last_error=? WHERE record_name=?", (err[:300], rn))
        P.execute("COMMIT")

    def _pending(self, limit):
        cur = self.progress.db.execute("""SELECT record_name, record_type, change_tag, zone, filename, taken
                                          FROM items WHERE status='pending' AND matched IS NOT NULL
                                          ORDER BY taken LIMIT ?""", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def test_one(self):
        item = self._pending(1)
        if not item:
            return True
        it = item[0]
        taken = datetime.fromtimestamp(it["taken"]).strftime("%Y-%m-%d %H:%M") if it["taken"] else "unknown date"
        say("\nStep 2 of 3: test with ONE photo.")
        if self.mode == "trash":
            say("  This PERMANENTLY deletes it from Recently Deleted. It cannot be undone.")
        say(f"  It will delete: {it['filename']}  (taken {taken}) - it's backed up on your PC.")
        if ask("Type DELETE ONE to do the test, or Q to quit").upper() != "DELETE ONE":
            raise Stop("Stopped before deleting anything.")
        ok, failed = self._delete_batch(item)
        self._record(ok, failed)
        if not ok:
            raise Stop(f"The test delete didn't go through ({next(iter(failed.values()), 'unknown')}). "
                       "Nothing else was touched.")
        say(f"\n  Done. Check that '{it['filename']}' {self.m['check']}.")
        if self.mode == "library":
            say("  (After a few minutes it should also be gone from the iPhone's Photos, if the phone still syncs.)")
        if ask("Type YES once you've checked, to continue with the rest, or Q to quit").upper() != "YES":
            raise Stop("Stopped after the test. Run again any time to continue.")
        self.progress.set_meta("test_ok", 1)
        return True

    def delete_all(self):
        pending, _, _ = self.summary()
        if not pending:
            return
        say(f"\nStep 3 of 3: {'PERMANENTLY deleting' if self.mode == 'trash' else 'deleting'} {pending:,} "
            f"backed-up photos from {self.m['where']}, {BATCH} at a time.")
        say("  Press Ctrl+C any time to pause safely; run again to continue.")
        done = 0
        since_break = 0
        start = time.monotonic()
        while True:
            batch = self._pending(BATCH)
            if not batch:
                break
            ok, failed = self._delete_batch(batch)
            self._record(ok, failed)
            done += len(ok)
            since_break += len(ok)
            el = time.monotonic() - start
            rate = done / el if el else 0
            remaining = self.summary()[0]
            line(f"  Deleted {done:,} of {pending:,}"
                 + (f"  |  {len(failed)} not accepted in last batch" if failed else "")
                 + (f"  |  about {human_duration(remaining / rate)} left" if rate and remaining else ""))
            if since_break >= 1000:
                since_break = 0
                time.sleep(PAUSE_PER_1000)
            else:
                time.sleep(PAUSE_BETWEEN)
        say("")

    def report(self):
        P = self.progress.db
        c = dict(P.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        unmatched = self.write_unmatched_report()
        lines = [
            f"iCloud Photo Cleaner {VERSION} - {datetime.now():%Y-%m-%d %H:%M}",
            f"Account: {self.apple_id}",
            "",
            f"{self.m['done']}: {c.get('deleted', 0):,}",
            f"Kept - no matching backup on the PC:                       {c.get('skipped', 0):,}",
            f"Not accepted by iCloud (will retry on next run):            {c.get('error', 0):,}",
            f"Still to delete:                                            {c.get('pending', 0):,}",
        ]
        if unmatched:
            lines.append(f"\nPhotos kept because no backup matched: see '{unmatched.name}'")
        (self.reports / self.m["report"]).write_text("\n".join(lines) + "\n", encoding="utf-8")
        say("\n" + "\n".join(lines))


# ============================================================================ main
def main():
    say("=" * 72)
    say(f" iCloud Photo Cleaner {VERSION} - deletes only photos already backed up on this PC")
    say("=" * 72)
    try:
        import pyicloud  # noqa: F401
    except ImportError:
        say("pyicloud isn't installed. Start this with 'Run iCloud Cleaner.bat'.")
        return 1

    settings = load_json(CLEANER_SETTINGS)
    dest = load_json(EXTRACTOR_SETTINGS).get("dest") or settings.get("dest") or "D:\\iPhone Photos"
    dest = ask("Backup folder", dest)
    try:
        say("\nChecking the backup...")
        backup = BackupIndex(dest)
        say(f"  {backup.count:,} backed-up library items found on the PC."
            + (f" ({backup.missing_on_disk:,} recorded but missing on disk - those will NOT be deleted.)"
               if backup.missing_on_disk else ""))
        apple_id = ask("Apple Account email", settings.get("apple_id"))
        if not apple_id:
            raise Stop("No Apple Account email entered.")
        default_mode = "2" if (Path(dest) / ".extractor" / MODES["library"]["db"]).exists() else "1"
        say("\nWhat should it do?")
        say(f"  1 = {MODES['library']['title']}")
        say(f"  2 = {MODES['trash']['title']}")
        choice = ask("Choose 1 or 2", default_mode)
        if choice not in ("1", "2"):
            raise Stop("Please run again and choose 1 or 2.")
        mode = "library" if choice == "1" else "trash"
        save_json(CLEANER_SETTINGS, {**settings, "apple_id": apple_id, "dest": dest})

        cleaner = Cleaner(dest, apple_id, mode)
        cleaner.sign_in()
        cleaner.scan(backup)
        pending, skipped, errors = cleaner.summary()
        say(f"\n  Matched to your backup (will be deleted):  {pending:,}")
        say(f"  No matching backup (will be KEPT):          {skipped:,}")
        if errors:
            say(f"  Not accepted last time (retrying):          {errors:,}")
            cleaner.progress.db.execute("UPDATE items SET status='pending' WHERE status='error'")
            pending += errors
        unmatched = cleaner.write_unmatched_report()
        if unmatched:
            say(f"  The kept ones are listed in: {unmatched}")
        if not pending:
            say("\nNothing to delete.")
            cleaner.report()
            return 0

        if cleaner.progress.meta("test_ok") != "1":
            cleaner.test_one()
        else:
            verb = "PERMANENTLY delete" if mode == "trash" else "Delete"
            if ask(f"\n{verb} {pending:,} backed-up photos from {cleaner.m['where']} now? Type YES").upper() != "YES":
                raise Stop("Stopped before deleting anything.")
        cleaner.delete_all()
        cleaner.report()
        say("\nFinished. " + ("Those photos are permanently gone from iCloud." if mode == "trash" else
                              "Everything deleted is in Recently Deleted on icloud.com for 30 days."))
        return 0
    except KeyboardInterrupt:
        say("\n\nPaused. Progress is saved; run again to continue.")
        try:
            cleaner.report()
        except Exception:
            pass
        return 0
    except Stop as e:
        say(f"\n{e}")
        try:
            cleaner.report()
        except Exception:
            pass
        return 2
    except Exception as e:
        say(f"\nUnexpected problem: {type(e).__name__}: {e}")
        say(traceback.format_exc(limit=4))
        say("Nothing beyond what's shown above was changed. Please report it (with this message) on the project's GitHub Issues page.")
        return 3


if __name__ == "__main__":
    code = main()
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
    sys.exit(code)
