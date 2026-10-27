#!/usr/bin/env python3
"""
iPhone Photo Extractor - window + supervisor.

The window shows progress. The supervisor runs the copy engine (extractor_core.py) as a separate
process and watches its once-per-second heartbeat. If the engine freezes (no heartbeat for 90 s) or
crashes, the supervisor kills it and starts a new one, which resumes from the ledger. The PC is
kept awake while a run is in progress.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_VERSION = "1.0"
HERE = Path(__file__).resolve().parent
CORE = HERE / "extractor_core.py"
SETTINGS = HERE / "extractor_settings.json"
HEARTBEAT_TIMEOUT = 90
QUICK_CRASH_SECONDS = 20
MAX_QUICK_CRASHES = 5
EXIT_COMPLETE, EXIT_FATAL, EXIT_DISK_FULL = 0, 3, 4


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def human_duration(seconds):
    if seconds is None:
        return "-"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data):
    try:
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def default_dest():
    s = load_settings().get("dest")
    if s:
        return s
    if sys.platform == "win32" and os.path.exists("D:\\"):
        return "D:\\iPhone Photos"
    return str(Path.home() / "Pictures" / "iPhone Photos")


def keep_awake(on):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


# ============================================================================ supervisor
class Supervisor(threading.Thread):
    """Runs the engine process, restarts it on freeze/crash, reports events to a queue."""

    def __init__(self, dest, events, retry_failed=False):
        super().__init__(daemon=True)
        self.dest = dest
        self.events = events
        self.retry_failed = retry_failed
        self._halt = threading.Event()
        self.proc = None

    def stop(self):
        self._halt.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
                p.wait(timeout=10)
            except Exception:
                pass

    def run(self):
        quick_crashes = 0
        restarts = 0
        first = True
        while not self._halt.is_set():
            cmd = [sys.executable, "-u", str(CORE), "--dest", self.dest]
            if first and self.retry_failed:
                cmd.append("--retry-failed")
            first = False
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            started = time.monotonic()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             encoding="utf-8", errors="replace", bufsize=1, env=env,
                                             creationflags=flags, cwd=str(HERE))
            except Exception as e:
                self.events.put({"t": "fatal", "msg": f"Could not start the copy engine: {e}"})
                return
            lines = queue.Queue()

            def reader(p=self.proc):
                for line in p.stdout:
                    lines.put(line)
                lines.put(None)

            threading.Thread(target=reader, daemon=True).start()
            last_beat = time.monotonic()
            tail = []
            fatal_msg = None
            froze = False
            while True:
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    line = ""
                if self._halt.is_set():
                    self._kill()
                    self.events.put({"t": "stopped"})
                    return
                if line is None:
                    break
                if line:
                    last_beat = time.monotonic()
                    line = line.strip()
                    try:
                        ev = json.loads(line)
                        if ev.get("t") == "fatal":
                            fatal_msg = ev.get("msg")
                        self.events.put(ev)
                    except ValueError:
                        tail = (tail + [line])[-20:]
                if time.monotonic() - last_beat > HEARTBEAT_TIMEOUT:
                    froze = True
                    self._kill()
                    break
            code = self.proc.wait()
            if self._halt.is_set():
                self.events.put({"t": "stopped"})
                return
            if froze:
                restarts += 1
                self.events.put({"t": "note", "msg": f"Engine froze; restarted it automatically (#{restarts}). "
                                                     "Finished files are safe."})
                time.sleep(3)
                continue
            if code == EXIT_COMPLETE:
                self.events.put({"t": "finished"})
                return
            if code in (EXIT_FATAL, EXIT_DISK_FULL):
                self.events.put({"t": "fatal", "msg": fatal_msg or "The copy engine stopped."})
                return
            lifetime = time.monotonic() - started
            quick_crashes = quick_crashes + 1 if lifetime < QUICK_CRASH_SECONDS else 0
            if quick_crashes >= MAX_QUICK_CRASHES:
                self.events.put({"t": "fatal", "msg": "The copy engine keeps crashing on start:\n" + "\n".join(tail[-8:])})
                return
            restarts += 1
            delay = min(5 * (2 ** min(quick_crashes, 4)), 60)
            self.events.put({"t": "note", "msg": f"Engine stopped unexpectedly; restarting in {delay}s (#{restarts}). "
                                                 "Finished files are safe."})
            for _ in range(delay):
                if self._halt.is_set():
                    self.events.put({"t": "stopped"})
                    return
                time.sleep(1)


# ============================================================================ window
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"iPhone Photo Extractor {APP_VERSION}")
    root.minsize(640, 470)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except Exception:
        pass

    events = queue.Queue()
    sup = {"t": None}
    last = {"snap": {}}

    pad = {"padx": 12, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="iPhone Photo Extractor", font=("Segoe UI", 15, "bold")).grid(row=0, column=0, columnspan=4,
                                                                                     sticky="w")
    device_var = tk.StringVar(value="Phone: not connected yet")
    ttk.Label(frm, textvariable=device_var, foreground="#555").grid(row=1, column=0, columnspan=4, sticky="w")

    ttk.Label(frm, text="Save to:").grid(row=2, column=0, sticky="w", pady=(12, 4))
    dest_var = tk.StringVar(value=default_dest())
    dest_entry = ttk.Entry(frm, textvariable=dest_var, width=44)
    dest_entry.grid(row=2, column=1, columnspan=2, sticky="we", pady=(12, 4))

    def browse():
        d = filedialog.askdirectory(initialdir=dest_var.get() or "C:\\", title="Choose the backup folder")
        if d:
            dest_var.set(os.path.normpath(d))

    browse_btn = ttk.Button(frm, text="Change...", command=browse)
    browse_btn.grid(row=2, column=3, sticky="e", pady=(12, 4))

    bar = ttk.Progressbar(frm, maximum=1000)
    bar.grid(row=3, column=0, columnspan=4, sticky="we", pady=(10, 2))
    pct_var = tk.StringVar(value="")
    ttk.Label(frm, textvariable=pct_var).grid(row=4, column=0, columnspan=4, sticky="w")

    grid = ttk.Frame(frm)
    grid.grid(row=5, column=0, columnspan=4, sticky="we", pady=(8, 4))
    fields = {}
    layout = [
        ("files", "Files copied"), ("data", "Data copied"),
        ("library", "Library items backed up"), ("speed", "Speed"),
        ("failed", "Failed (will retry)"), ("eta", "Time left"),
        ("dups", "Identical copies skipped"), ("icloud", "Only in iCloud (not on phone)"),
    ]
    for i, (key, label) in enumerate(layout):
        r, c = divmod(i, 2)
        ttk.Label(grid, text=label + ":", foreground="#555").grid(row=r, column=c * 2, sticky="w", padx=(0, 6), pady=2)
        v = tk.StringVar(value="-")
        ttk.Label(grid, textvariable=v, font=("Segoe UI", 10, "bold")).grid(row=r, column=c * 2 + 1, sticky="w",
                                                                           padx=(0, 20), pady=2)
        fields[key] = v

    current_var = tk.StringVar(value="")
    ttk.Label(frm, textvariable=current_var, foreground="#555").grid(row=6, column=0, columnspan=4, sticky="w",
                                                                     pady=(6, 0))
    status_var = tk.StringVar(value="Plug in the iPhone, unlock it, then press Start.")
    ttk.Label(frm, textvariable=status_var, wraplength=600, justify="left").grid(row=7, column=0, columnspan=4,
                                                                                 sticky="w", pady=(6, 6))

    btns = ttk.Frame(frm)
    btns.grid(row=8, column=0, columnspan=4, sticky="we", pady=(8, 0))
    start_btn = ttk.Button(btns, text="Start")
    stop_btn = ttk.Button(btns, text="Stop", state="disabled")
    retry_btn = ttk.Button(btns, text="Retry failed", state="disabled")
    folder_btn = ttk.Button(btns, text="Open backup folder")
    report_btn = ttk.Button(btns, text="Open report")
    for b in (start_btn, stop_btn, retry_btn, folder_btn, report_btn):
        b.pack(side="left", padx=(0, 8))
    frm.columnconfigure(1, weight=1)

    def set_running(running):
        start_btn.config(state="disabled" if running else "normal")
        stop_btn.config(state="normal" if running else "disabled")
        dest_entry.config(state="disabled" if running else "normal")
        browse_btn.config(state="disabled" if running else "normal")
        failed = (last["snap"].get("failed") or 0) > 0
        retry_btn.config(state="normal" if (failed and not running) else "disabled")
        keep_awake(running)

    def start(retry=False):
        dest = dest_var.get().strip()
        if not dest:
            messagebox.showwarning("Backup folder", "Choose where to save the photos first.")
            return
        try:
            Path(dest).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Backup folder", f"Can't use that folder:\n{e}")
            return
        save_settings({**load_settings(), "dest": dest})
        status_var.set("Starting...")
        s = Supervisor(dest, events, retry_failed=retry)
        sup["t"] = s
        s.start()
        set_running(True)

    def stop():
        if sup["t"]:
            status_var.set("Stopping... (finished files are kept; press Start to continue later)")
            sup["t"].stop()

    def open_path(p):
        p = Path(p)
        if not p.exists():
            messagebox.showinfo("Not there yet", f"{p} doesn't exist yet.")
            return
        if sys.platform == "win32":
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])

    start_btn.config(command=start)
    stop_btn.config(command=stop)
    retry_btn.config(command=lambda: start(retry=True))
    folder_btn.config(command=lambda: open_path(dest_var.get()))
    report_btn.config(command=lambda: open_path(Path(dest_var.get()) / "_Export Reports" / "Export Report.txt"))

    def show(snap):
        last["snap"] = snap
        if snap.get("device"):
            device_var.set(f"Phone: {snap['device']}")
        tf, df = snap.get("total_files", 0), snap.get("done_files", 0)
        tb, db = snap.get("total_bytes", 0), snap.get("done_bytes", 0)
        if tb:
            frac = min(db / tb, 1.0)
            bar["value"] = frac * 1000
            pct_var.set(f"{frac * 100:,.1f}%")
        fields["files"].set(f"{df:,} / {tf:,}")
        fields["data"].set(f"{human_bytes(db)} / {human_bytes(tb)}")
        if "library_items" in snap:
            fields["library"].set(f"{snap['library_backed_up']:,} / {snap['library_on_phone']:,} on phone")
            fields["icloud"].set(f"{snap['not_on_phone']:,}")
        fields["speed"].set(f"{snap.get('speed', 0) / 1024 ** 2:,.1f} MB/s" if snap.get("speed") else "-")
        fields["eta"].set(human_duration(snap.get("eta")) if snap.get("state") == "copying" else "-")
        fields["failed"].set(f"{snap.get('failed', 0):,}")
        fields["dups"].set(f"{snap.get('duplicates', 0):,}")
        current_var.set(f"Now: {snap['current']}" if snap.get("current") else "")
        if snap.get("msg") and snap.get("state") != "copying":
            status_var.set(snap["msg"])
        elif snap.get("state") == "copying":
            status_var.set("Copying. You can leave this running; it recovers from freezes and unplugging by itself.")

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                t = ev.get("t")
                if t == "status":
                    show(ev)
                elif t == "note":
                    status_var.set(ev["msg"])
                elif t == "finished":
                    set_running(False)
                    snap = last["snap"]
                    status_var.set(snap.get("msg") or "Finished.")
                    current_var.set("")
                    messagebox.showinfo("Finished", (snap.get("msg") or "Finished.")
                                        + "\n\nThe full report is in the '_Export Reports' folder.")
                elif t == "fatal":
                    set_running(False)
                    status_var.set(ev.get("msg", "Stopped."))
                    messagebox.showerror("Stopped", ev.get("msg", "Stopped."))
                elif t == "crash":
                    status_var.set(f"Engine problem: {ev.get('msg')} - restarting automatically.")
                elif t == "stopped":
                    set_running(False)
                    status_var.set("Stopped. Finished files are kept; press Start to continue.")
                    current_var.set("")
        except queue.Empty:
            pass
        root.after(250, pump)

    def on_close():
        if sup["t"] and sup["t"].is_alive():
            if not messagebox.askyesno("Stop and close?", "A backup is running. Stop it and close?\n\n"
                                                          "Finished files are kept; next time it continues where "
                                                          "it left off."):
                return
            sup["t"].stop()
        keep_awake(False)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(250, pump)
    root.mainloop()


# ============================================================================ console fallback
def run_console():
    dest = default_dest()
    print(f"iPhone Photo Extractor {APP_VERSION} (console mode)\nSaving to: {dest}\nPress Ctrl+C to stop.\n")
    Path(dest).mkdir(parents=True, exist_ok=True)
    events = queue.Queue()
    s = Supervisor(dest, events, retry_failed="--retry-failed" in sys.argv)
    s.start()
    keep_awake(True)
    try:
        while True:
            ev = events.get()
            t = ev.get("t")
            if t == "status":
                tb = ev.get("total_bytes") or 1
                line = (f"{ev.get('done_bytes', 0) / tb * 100:5.1f}%  files {ev.get('done_files', 0):,}/"
                        f"{ev.get('total_files', 0):,}  failed {ev.get('failed', 0):,}  "
                        f"{(ev.get('speed') or 0) / 1024 ** 2:5.1f} MB/s  left {human_duration(ev.get('eta'))}  "
                        f"| {ev.get('msg', '')}")
                sys.stdout.write("\r" + line[:150].ljust(150))
                sys.stdout.flush()
            elif t in ("note", "crash"):
                print("\n" + ev.get("msg", ""))
            elif t in ("finished", "fatal", "stopped"):
                print("\n" + (ev.get("msg") or t))
                break
    except KeyboardInterrupt:
        s.stop()
    finally:
        keep_awake(False)


if __name__ == "__main__":
    if "--console" in sys.argv:
        run_console()
    else:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print("The window toolkit (tkinter) isn't available in this Python; using console mode.")
            run_console()
        else:
            run_gui()
