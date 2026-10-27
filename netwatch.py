#!/usr/bin/env python3
"""
iPhone Network Watch  (read-only)

Shows, live over the USB cable, which apps and system processes on the iPhone are sending
and receiving data, and which servers they are talking to. Works on Wi-Fi and cellular
traffic alike. Does not need App Privacy Report or developer mode.

What it can and can't see:
  * It sees WHO (the app/process name iOS attaches to every packet), WHERE (server address,
    and its name when it can be learned from DNS lookups or the TLS handshake), HOW MUCH and WHEN.
  * It cannot read WHAT was sent: nearly all app traffic is encrypted.
  * It only sees traffic while the phone is plugged in and this program is running.

SAFETY: It only listens. It sends nothing to the phone except the request to start the
packet mirror, and changes no setting, app or file on the phone. Captured packet contents
are not saved; only per-app/per-server totals are written to the netwatch_output folder.
"""

import asyncio
import csv
import ipaddress
import os
import socket
import struct
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

NETWATCH_VERSION = "1.0"
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "netwatch_output"
APPLE_MUX_PORT = 27015
REFRESH_SECONDS = 2
AUTOSAVE_SECONDS = 60
MAX_DESTS_PER_APP = 500
TOP_APPS_ON_SCREEN = 25
FAKE_ETHERNET = b"\xbe\xef" * 6 + b"\x08\x00"

WELL_KNOWN = {53: "DNS", 80: "HTTP", 443: "HTTPS/QUIC", 853: "DNS-over-TLS", 993: "IMAP", 587: "SMTP",
              465: "SMTP", 5223: "Apple Push", 123: "NTP", 3478: "STUN/call", 5228: "Google Push",
              1900: "SSDP", 5353: "mDNS"}


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def human_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


# ============================================================================ packet parsing
def find_ip(data, family):
    """Return the IP packet inside the captured frame (handles real/fake Ethernet and 4-byte loopback headers)."""
    want = 4 if family == 2 else 6 if family == 30 else None
    for off in (14, 4, 0, 18):
        if len(data) <= off:
            continue
        ver = data[off] >> 4
        if want is not None and ver != want:
            continue
        if ver == 4 and len(data) >= off + 20:
            total = struct.unpack_from("!H", data, off + 2)[0]
            if 20 <= total <= len(data) - off + 4:
                return data[off:]
        if ver == 6 and len(data) >= off + 40:
            plen = struct.unpack_from("!H", data, off + 4)[0]
            if plen <= len(data) - off - 40 + 4:
                return data[off:]
    return None


def parse_ip(ip):
    """-> (src, dst, proto, sport, dport, tcp_flags, payload, length) or None."""
    ver = ip[0] >> 4
    if ver == 4:
        ihl = (ip[0] & 15) * 4
        length = struct.unpack_from("!H", ip, 2)[0]
        proto = ip[9]
        src = socket.inet_ntop(socket.AF_INET, ip[12:16])
        dst = socket.inet_ntop(socket.AF_INET, ip[16:20])
        l4 = ip[ihl:length] if length <= len(ip) else ip[ihl:]
    elif ver == 6:
        length = 40 + struct.unpack_from("!H", ip, 4)[0]
        proto = ip[6]
        src = socket.inet_ntop(socket.AF_INET6, ip[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, ip[24:40])
        l4 = ip[40:length]
    else:
        return None
    sport = dport = flags = 0
    payload = b""
    if proto == 6 and len(l4) >= 20:
        sport, dport = struct.unpack_from("!HH", l4, 0)
        off = (l4[12] >> 4) * 4
        flags = l4[13]
        payload = l4[off:]
    elif proto == 17 and len(l4) >= 8:
        sport, dport = struct.unpack_from("!HH", l4, 0)
        payload = l4[8:]
    return src, dst, proto, sport, dport, flags, payload, length


def _dns_name(msg, off, depth=0):
    labels = []
    jumped_end = None
    while off < len(msg) and depth < 20:
        n = msg[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack_from("!H", msg, off)[0] & 0x3FFF
            if jumped_end is None:
                jumped_end = off + 2
            off = ptr
            depth += 1
            continue
        labels.append(msg[off + 1:off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels), (jumped_end if jumped_end is not None else off)


def parse_dns_answers(msg):
    """-> (question_name, [ip, ...]) from a DNS response."""
    if len(msg) < 12:
        return None, []
    flags, qd, an = struct.unpack_from("!HHH", msg, 2)
    if not flags & 0x8000:
        return None, []
    off = 12
    qname = None
    for _ in range(qd):
        name, off = _dns_name(msg, off)
        qname = qname or name
        off += 4
    ips = []
    for _ in range(an):
        _, off = _dns_name(msg, off)
        if off + 10 > len(msg):
            break
        rtype, _, _, rdlen = struct.unpack_from("!HHIH", msg, off)
        off += 10
        rdata = msg[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntop(socket.AF_INET, rdata))
        elif rtype == 28 and rdlen == 16:
            ips.append(socket.inet_ntop(socket.AF_INET6, rdata))
    return qname, ips


def parse_sni(p):
    """Server name from a TLS ClientHello (the one unencrypted name in an HTTPS connection)."""
    if len(p) < 44 or p[0] != 0x16 or p[5] != 0x01:
        return None
    off = 9 + 2 + 32
    off += 1 + p[off]
    off += 2 + struct.unpack_from("!H", p, off)[0]
    off += 1 + p[off]
    end = off + 2 + struct.unpack_from("!H", p, off)[0]
    off += 2
    while off + 4 <= min(end, len(p)):
        etype, elen = struct.unpack_from("!HH", p, off)
        off += 4
        if etype == 0 and elen >= 5:
            nlen = struct.unpack_from("!H", p, off + 3)[0]
            return p[off + 5:off + 5 + nlen].decode("ascii", "replace")
        off += elen
    return None


def parse_http_host(p):
    if not p[:8].split(b" ")[0] in (b"GET", b"POST", b"PUT", b"HEAD", b"DELETE", b"PATCH", b"OPTIONS"):
        return None
    for line in p[:2048].split(b"\r\n")[1:]:
        if line[:5].lower() == b"host:":
            return line[5:].strip().decode("ascii", "replace")
    return None


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_link_local or a.is_multicast or a.is_loopback
    except ValueError:
        return False


# ============================================================================ aggregation
class Stats:
    def __init__(self):
        self.started = time.time()
        self.apps = {}               # app -> dict
        self.names = {}              # ip -> hostname
        self.local_ips = set()
        self.io_votes = {}           # io byte -> Counter(out/in)
        self.packets = 0
        self.unparsed = 0
        self.by_iface = Counter()
        self.reconnects = 0
        self.last_error = None

    def _app(self, name):
        a = self.apps.get(name)
        if a is None:
            a = self.apps[name] = {"out": 0, "in": 0, "unk": 0, "pkts": 0,
                                   "first": time.time(), "last": 0, "dests": {}}
        return a

    def _direction(self, src, dst, io):
        if src in self.local_ips and dst not in self.local_ips:
            d = "out"
        elif dst in self.local_ips and src not in self.local_ips:
            d = "in"
        else:
            votes = self.io_votes.get(io)
            if votes and sum(votes.values()) >= 20:
                return votes.most_common(1)[0][0]
            return "unk"
        self.io_votes.setdefault(io, Counter())[d] += 1
        return d

    def add(self, pkt):
        self.packets += 1
        iface = getattr(pkt, "interface_name", "") or "?"
        ip = find_ip(pkt.data, int(pkt.protocol_family))
        if ip is None:
            self.unparsed += 1
            return
        parsed = parse_ip(ip)
        if not parsed:
            self.unparsed += 1
            return
        src, dst, proto, sport, dport, flags, payload, length = parsed
        self.by_iface["Wi-Fi" if iface.startswith("en") else "Cellular" if iface.startswith("pdp") else iface] += length

        # learn the phone's own addresses from traffic only the phone would originate
        if proto == 17 and dport in (53, 853) and sport not in (53, 853):
            self.local_ips.add(src)
        elif proto == 17 and sport == 53:
            self.local_ips.add(dst)
            try:
                qname, ips = parse_dns_answers(payload)
                for a in ips:
                    if qname:
                        self.names[a] = qname
            except Exception:
                pass
        if proto == 6 and flags & 0x02 and not flags & 0x10:          # SYN without ACK: phone opens connection
            self.local_ips.add(src)
        if proto == 6 and payload:
            try:
                host = parse_sni(payload) or parse_http_host(payload)
            except Exception:
                host = None
            if host:
                self.local_ips.add(src)
                self.names[dst] = host

        direction = self._direction(src, dst, getattr(pkt, "io", -1))
        remote, rport = (dst, dport) if direction == "out" else (src, sport) if direction == "in" else (
            (dst, dport) if is_private(src) else (src, sport))

        comm = (pkt.comm or "").strip() or f"pid {pkt.pid}"
        ecomm = (pkt.ecomm or "").strip()
        app = f"{ecomm} (via {comm})" if ecomm and ecomm != comm else comm

        a = self._app(app)
        a[direction] += length
        a["pkts"] += 1
        a["last"] = time.time()
        d = a["dests"].get(remote)
        if d is None:
            if len(a["dests"]) >= MAX_DESTS_PER_APP:
                remote = "(other servers)"
                d = a["dests"].get(remote)
            if d is None:
                d = a["dests"][remote] = {"out": 0, "in": 0, "unk": 0, "pkts": 0, "ports": set(),
                                          "first": time.time(), "last": 0}
        d[direction] += length
        d["pkts"] += 1
        d["last"] = time.time()
        if len(d["ports"]) < 8:
            d["ports"].add(f"{'TCP' if proto == 6 else 'UDP' if proto == 17 else proto}/{rport}")

    def label(self, ip):
        return self.names.get(ip, ip)

    def ranked(self):
        return sorted(self.apps.items(), key=lambda kv: -(kv[1]["out"] + kv[1]["unk"] / 2))


# ============================================================================ output
def render(stats, status):
    el = time.time() - stats.started
    total = sum(stats.by_iface.values())
    lines = [
        f" iPhone Network Watch v{NETWATCH_VERSION}  |  watching {human_duration(el)}  |  {status}",
        f" {stats.packets:,} packets, {human_bytes(total)} total  |  "
        + "  ".join(f"{k}: {human_bytes(v)}" for k, v in stats.by_iface.most_common())
        + f"  |  {len(stats.apps)} apps/processes seen",
        " Press Ctrl+C to stop and save the report.  (Report also auto-saves every minute.)",
        "-" * 118,
        f" {'APP / PROCESS':<34}{'SENT':>11}{'RECEIVED':>11}{'SERVERS':>9}   TOP DESTINATION (by data sent)",
        "-" * 118,
    ]
    for name, a in stats.ranked()[:TOP_APPS_ON_SCREEN]:
        top = max(a["dests"].items(), key=lambda kv: kv[1]["out"] + kv[1]["unk"] / 2, default=(None, None))[0]
        lines.append(f" {name[:33]:<34}{human_bytes(a['out'] + a['unk'] / 2):>11}{human_bytes(a['in'] + a['unk'] / 2):>11}"
                     f"{len(a['dests']):>9}   {(stats.label(top) if top else '')[:46]}")
    if len(stats.apps) > TOP_APPS_ON_SCREEN:
        lines.append(f" ... and {len(stats.apps) - TOP_APPS_ON_SCREEN} more in the saved report")
    if stats.last_error:
        lines.append("")
        lines.append(f" Last connection issue: {stats.last_error}")
    sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + "\n")
    sys.stdout.flush()


def save_report(stats, session_tag, device):
    OUT_DIR.mkdir(exist_ok=True)
    el = time.time() - stats.started
    txt = OUT_DIR / f"network_report_{session_tag}.txt"
    csv_path = OUT_DIR / f"network_destinations_{session_tag}.csv"

    out = ["=" * 90,
           f"iPhone Network Watch v{NETWATCH_VERSION}   started {datetime.fromtimestamp(stats.started):%Y-%m-%d %H:%M:%S}",
           f"iPhone: {device}   |   watched for {human_duration(el)}   |   {stats.packets:,} packets",
           "Traffic by connection: " + ", ".join(f"{k} {human_bytes(v)}" for k, v in stats.by_iface.most_common()),
           "SENT = data leaving the phone. Server names come from the phone's own DNS lookups / TLS handshakes;",
           "a bare IP address means the name could not be seen (e.g. encrypted DNS or iCloud Private Relay).",
           "=" * 90, ""]
    for name, a in stats.ranked():
        out.append(f"{name}")
        out.append(f"   sent {human_bytes(a['out'] + a['unk'] / 2)}, received {human_bytes(a['in'] + a['unk'] / 2)}, "
                   f"{a['pkts']:,} packets, {len(a['dests'])} servers, "
                   f"active {datetime.fromtimestamp(a['first']):%H:%M:%S} - {datetime.fromtimestamp(a['last']):%H:%M:%S}")
        for ip, d in sorted(a["dests"].items(), key=lambda kv: -(kv[1]["out"] + kv[1]["unk"] / 2))[:10]:
            out.append(f"      -> {stats.label(ip)[:55]:<56} sent {human_bytes(d['out'] + d['unk'] / 2):>10}"
                       f"   recv {human_bytes(d['in'] + d['unk'] / 2):>10}   {', '.join(sorted(d['ports']))}")
        if len(a["dests"]) > 10:
            out.append(f"      ... {len(a['dests']) - 10} more servers in the CSV")
        out.append("")
    tmp = txt.with_suffix(".tmp")
    tmp.write_text("\n".join(out), encoding="utf-8")
    os.replace(tmp, txt)

    tmpc = csv_path.with_suffix(".tmp")
    with open(tmpc, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["app_or_process", "server_name", "server_ip", "bytes_sent", "bytes_received",
                    "bytes_direction_unknown", "packets", "ports", "first_seen", "last_seen"])
        for name, a in stats.ranked():
            for ip, d in a["dests"].items():
                w.writerow([name, stats.names.get(ip, ""), ip, d["out"], d["in"], d["unk"], d["pkts"],
                            " ".join(sorted(d["ports"])),
                            datetime.fromtimestamp(d["first"]).isoformat(timespec="seconds"),
                            datetime.fromtimestamp(d["last"]).isoformat(timespec="seconds") if d["last"] else ""])
    os.replace(tmpc, csv_path)
    return txt, csv_path


# ============================================================================ connection
def apple_service_running():
    if sys.platform != "win32":
        return True
    try:
        with socket.create_connection(("127.0.0.1", APPLE_MUX_PORT), timeout=3):
            return True
    except OSError:
        return False


async def connect(say):
    from pymobiledevice3 import usbmux
    from pymobiledevice3.exceptions import (
        PairingDialogResponsePendingError, PasswordRequiredError, NotPairedError, UserDeniedPairingError,
    )
    from pymobiledevice3.lockdown import create_using_usbmux
    while True:
        try:
            usb = [d for d in await usbmux.list_devices() if d.is_usb]
            if not usb:
                say("Waiting for the iPhone on USB (plug it in and unlock it)...")
            else:
                return await create_using_usbmux(serial=usb[0].serial, connection_type="USB", pair_timeout=60)
        except PasswordRequiredError:
            say("iPhone is locked: unlock it once so it can reconnect.")
        except (PairingDialogResponsePendingError, NotPairedError):
            say("Tap 'Trust' on the iPhone and enter your passcode.")
        except UserDeniedPairingError:
            say("'Don't Trust' was tapped. Unplug, plug back in, and tap 'Trust'.")
        except Exception as e:
            say(f"Connecting... ({type(e).__name__})")
        await asyncio.sleep(3)


async def run(stats, session_tag, state):
    from pymobiledevice3.services.pcapd import PcapdService

    def say(msg):
        state["status"] = msg

    async def screen():
        last_save = time.time()
        while True:
            render(stats, state["status"])
            if time.time() - last_save >= AUTOSAVE_SECONDS and stats.packets:
                save_report(stats, session_tag, state.get("device", "?"))
                last_save = time.time()
            await asyncio.sleep(REFRESH_SECONDS)

    ui = asyncio.create_task(screen())
    backoff = 3
    try:
        while True:
            lockdown = await connect(say)
            info = lockdown.all_values
            state["device"] = f"{info.get('ProductType')} iOS {info.get('ProductVersion')}"
            say(f"Watching {state['device']}")
            try:
                async with PcapdService(lockdown) as svc:
                    backoff = 3
                    got_any = False
                    async for pkt in svc.watch():
                        got_any = True
                        try:
                            stats.add(pkt)
                        except Exception:
                            stats.unparsed += 1
                    if not got_any:
                        stats.last_error = "Packet mirror ended without sending data"
            except Exception as e:
                stats.last_error = f"{datetime.now():%H:%M:%S} {type(e).__name__}: {e}"[:160]
            finally:
                try:
                    await lockdown.close()
                except Exception:
                    pass
            stats.reconnects += 1
            say(f"Connection dropped; reconnecting in {backoff}s (data so far is kept)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
    finally:
        ui.cancel()


def main():
    if sys.platform == "win32":
        os.system("")  # enable ANSI screen refresh in the Windows console
    try:
        from pymobiledevice3.utils import get_asyncio_loop
    except ImportError:
        print("pymobiledevice3 is not installed. Run 'Run Network Watch.bat' instead of this file directly.")
        sys.exit(1)
    if not apple_service_running():
        print("Apple Mobile Device Service is not running. Install 'Apple Devices' from the Microsoft Store, "
              "restart, and try again.")
        input("Press Enter to close...")
        sys.exit(1)

    stats = Stats()
    session_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    state = {"status": "Starting..."}
    try:
        get_asyncio_loop().run_until_complete(run(stats, session_tag, state))
    except KeyboardInterrupt:
        pass
    except Exception:
        stats.last_error = traceback.format_exc(limit=3)
    txt, csv_path = save_report(stats, session_tag, state.get("device", "?"))
    print("\n\nStopped.")
    if stats.packets == 0:
        print("No traffic was captured.")
        if stats.last_error:
            print(f"Last error: {stats.last_error}")
    print(f"Report: {txt}")
    print(f"Full list of every app -> server: {csv_path}")


if __name__ == "__main__":
    main()
