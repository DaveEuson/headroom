"""Wi-Fi manager for Headroom (runs as root, needs NetworkManager).

Two jobs:

1. **Setup hotspot.** On boot with no network (or if the network drops for
   a while), scan nearby networks, start a WPA2 hotspot
   "Headroom-Setup", and serve a captive portal on http://10.42.0.1
   where the user picks their home network. While in hotspot mode we
   periodically drop the hotspot to see if a known network came back.

2. **Control API** on 127.0.0.1:8079 (localhost only) so the dashboard can
   list networks and switch Wi-Fi at any time, even while connected:
       GET  /networks -> {"current": ssid|null, "networks": [...]}
       GET  /status   -> {"phase": ..., "error": ..., "current": ...}
       POST /join     {"ssid": ..., "password": ...} -> {"ok": true}

State for the HAT display is written to /run/headroom/wifi.json.
Standard library + nmcli only.
"""

import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOTSPOT_SSID = "Headroom-Setup"
HOTSPOT_PSK = "headroom"          # printed on the device screen
HOTSPOT_CON = "ctp-hotspot"
PORTAL_IP = "10.42.0.1"           # NetworkManager's shared-mode gateway
CONTROL_PORT = 8079               # localhost API for the dashboard

FIRST_BOOT_GRACE = 45             # no saved wifi at all -> hotspot quickly
RECONNECT_GRACE = 300             # known wifi just down -> be patient first
RECHECK_HOTSPOT = 120             # while hosting: retry known networks
JOIN_TIMEOUT = 60

STATE_DIR = "/run/headroom"
STATE_FILE = os.path.join(STATE_DIR, "wifi.json")

PROBE_PATHS = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/ncsi.txt",
    "/connecttest.txt", "/success.txt", "/canonical.html",
    "/library/test/success.html",
}

_scanned = []                     # [(ssid, signal, secured)]
_status = {"phase": "starting", "error": None}
_lock = threading.Lock()

MAX_BODY = 65536                  # cap request bodies (little RAM)
_scan_cache = (0.0, [])           # (timestamp, networks) for the control API


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def valid_ssid(ssid):
    """Reject empty, over-long, option-like, or control-char SSIDs before we
    hand them to nmcli as argv (avoids a leading '-' being read as a flag)."""
    return bool(ssid) and not ssid.startswith("-") and len(ssid) <= 64 \
        and all(ord(c) >= 32 for c in ssid)


def _set_status(**kw):
    with _lock:
        _status.update(kw)


def nmcli(*args, timeout=30):
    return subprocess.run(
        ["nmcli", *args], capture_output=True, text=True, timeout=timeout
    )


def hotspot_active():
    return HOTSPOT_CON in nmcli(
        "-t", "-f", "NAME", "connection", "show", "--active"
    ).stdout


def current_ssid():
    """SSID we're connected to as a client, or None."""
    if hotspot_active():
        return None
    r = nmcli("-t", "-f", "ACTIVE,SSID", "device", "wifi", "list")
    for line in r.stdout.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes":
            return ssid.replace("\\:", ":") or None
    return None


def wifi_connected():
    if hotspot_active():
        return False
    r = nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device")
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] in ("wifi", "ethernet") \
                and parts[2].startswith("connected"):
            return True
    return False


def saved_wifi_profiles():
    """Names of saved wifi connections (excluding our hotspot)."""
    r = nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    names = []
    for line in r.stdout.splitlines():
        name, _, ctype = line.rpartition(":")
        if ctype == "802-11-wireless" and name != HOTSPOT_CON:
            names.append(name)
    return names


def scan_networks():
    nmcli("device", "wifi", "rescan", timeout=20)
    time.sleep(4)
    r = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
    best = {}
    for line in r.stdout.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 3:
            continue
        ssid = parts[0].replace("\\:", ":").strip()
        if not ssid or ssid == HOTSPOT_SSID:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        secured = parts[2].strip() not in ("", "--")
        if ssid not in best or signal > best[ssid][0]:
            best[ssid] = (signal, secured)
    nets = [(s, sig, sec) for s, (sig, sec) in best.items()]
    nets.sort(key=lambda n: -n[1])
    return nets[:15]


def scan_cached(max_age=15):
    """Wi-Fi scan for the control API, cached briefly so repeated dashboard
    polls don't each trigger a blocking rescan."""
    global _scan_cache
    if time.time() - _scan_cache[0] < max_age and _scan_cache[1]:
        return _scan_cache[1]
    nets = scan_networks()
    _scan_cache = (time.time(), nets)
    return nets


def write_state(**extra):
    os.makedirs(STATE_DIR, exist_ok=True)
    data = {
        "mode": "hotspot",
        "ssid": HOTSPOT_SSID,
        "password": HOTSPOT_PSK,
        "url": f"http://{PORTAL_IP}",
    }
    data.update(extra)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, STATE_FILE)


def clear_state():
    try:
        os.unlink(STATE_FILE)
    except OSError:
        pass


def start_hotspot():
    nmcli("connection", "delete", HOTSPOT_CON)
    r = nmcli("device", "wifi", "hotspot", "ifname", "wlan0",
              "con-name", HOTSPOT_CON, "ssid", HOTSPOT_SSID,
              "password", HOTSPOT_PSK, timeout=45)
    if r.returncode != 0:
        log(f"hotspot failed: {r.stderr.strip()}")
        return False
    log(f"hotspot '{HOTSPOT_SSID}' up; portal at http://{PORTAL_IP}")
    return True


def stop_hotspot(delete=True):
    nmcli("connection", "down", HOTSPOT_CON)
    if delete:
        nmcli("connection", "delete", HOTSPOT_CON)


def join_network(ssid, password, rescue_hotspot):
    """Join a network. On failure: restore hotspot (if we were hosting) or
    let NetworkManager fall back to the previous known network."""
    existed_before = ssid in saved_wifi_profiles()
    _set_status(phase="joining", error=None, target=ssid)
    if rescue_hotspot:
        write_state(joining=ssid)
        stop_hotspot(delete=False)
    args = ["device", "wifi", "connect", ssid, "ifname", "wlan0"]
    if password:
        args += ["password", password]
    r = nmcli(*args, timeout=JOIN_TIMEOUT)
    if r.returncode == 0 and wifi_connected():
        log(f"joined '{ssid}'")
        stop_hotspot()
        clear_state()
        _set_status(phase="connected", error=None)
        return True
    error = (r.stderr or r.stdout).strip().splitlines()
    error = error[-1] if error else "unknown error"
    log(f"join '{ssid}' failed: {error}")
    if not existed_before and ssid != HOTSPOT_CON:
        # don't leave a broken new profile around to auto-retry forever
        # (never our own hotspot connection)
        nmcli("connection", "delete", ssid)
    msg = f"Couldn't join {ssid} — wrong password?"
    _set_status(phase="hotspot" if rescue_hotspot else "connected", error=msg)
    if rescue_hotspot:
        start_hotspot()
        write_state(error=msg)
    return False


# ------------------------------------------------------------ portal (:80)

PORTAL_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Headroom Wi-Fi setup</title>
<style>
 * { box-sizing:border-box; }
 body { margin:0; padding:24px 18px 40px; font-family:system-ui,-apple-system,
   "Segoe UI",sans-serif; background:#f0eee6; color:#3d3929; line-height:1.5; }
 .wrap { max-width:440px; margin:0 auto; }
 h1 { font-size:1.4rem; margin:0 0 4px; }
 .muted { color:#8a8478; font-size:.9rem; }
 .card { background:#faf9f5; border:1px solid rgba(61,57,41,.12);
   border-radius:14px; padding:16px; margin-top:14px; }
 label.net { display:flex; align-items:center; gap:10px; padding:10px 6px;
   border-bottom:1px solid rgba(61,57,41,.08); font-size:1rem; }
 label.net:last-of-type { border-bottom:none; }
 .sig { margin-left:auto; color:#8a8478; font-size:.8rem; }
 input[type=text],input[type=password] { width:100%; padding:12px;
   font-size:1rem; border-radius:10px; border:1px solid rgba(61,57,41,.25);
   background:#fff; margin:6px 0 12px; }
 .btn { display:block; width:100%; text-align:center; background:#d97757;
   color:#fff; font-weight:600; font-size:1.05rem; padding:14px;
   border-radius:10px; border:none; cursor:pointer; }
 .err { color:#c4453d; font-weight:600; margin-top:10px; }
</style></head>
<body><div class="wrap">
 <h1>Connect your tracker to Wi-Fi</h1>
 <p class="muted">Pick your home network. The tracker will hop onto it and
   this setup network will disappear — that means it worked.</p>
 __ERROR__
 <form method="POST" action="/join"><div class="card">
   __NETWORKS__
   <div style="margin-top:12px">
     <input type="text" name="ssid_other" placeholder="Or type a network name">
     <input type="password" name="password" placeholder="Wi-Fi password">
     <button class="btn" type="submit">Connect</button>
   </div>
 </div></form>
 <p class="muted" style="margin-top:14px">After it connects, look at the
   tracker's little screen — it will show a new QR code to finish setup.</p>
</div></body></html>"""

JOINING_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connecting…</title></head>
<body style="font-family:system-ui;padding:32px 20px;background:#f0eee6;color:#3d3929">
<h2>Connecting to __SSID__…</h2>
<p>The <b>Headroom-Setup</b> network will now disappear.</p>
<p><b>Watch the tracker's screen:</b> if it shows a QR code for
"Set me up", it's online — reconnect your phone to your own Wi-Fi and scan it.
If the setup network comes back instead, the password didn't work — rejoin it
and try again.</p></body></html>"""


def _network_rows():
    rows = []
    for ssid, signal, secured in _scanned:
        safe = html.escape(ssid, quote=True)
        lock = "&#128274; " if secured else ""
        rows.append(
            f'<label class="net"><input type="radio" name="ssid" '
            f'value="{safe}">{lock}{html.escape(ssid)}'
            f'<span class="sig">{signal}%</span></label>'
        )
    return "\n".join(rows) or \
        '<p class="muted">No networks found — type yours below.</p>'


def make_portal_handler():
    class Portal(BaseHTTPRequestHandler):
        server_version = "HeadroomSetup/1.0"

        def _page(self, body, code=200):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            host = (self.headers.get("Host") or "").split(":")[0]
            if path in PROBE_PATHS or host not in (PORTAL_IP, ""):
                self.send_response(302)
                self.send_header("Location", f"http://{PORTAL_IP}/")
                self.end_headers()
                return
            with _lock:
                error = _status.get("error")
            banner = f'<p class="err">{html.escape(error)}</p>' if error else ""
            self._page(PORTAL_PAGE.replace("__NETWORKS__", _network_rows())
                       .replace("__ERROR__", banner))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                self._page("Request too large.", code=413)
                return
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace"))
            ssid = (form.get("ssid_other", [""])[0].strip()
                    or form.get("ssid", [""])[0].strip())
            password = form.get("password", [""])[0]
            if not valid_ssid(ssid):
                self._page(PORTAL_PAGE
                           .replace("__NETWORKS__", _network_rows())
                           .replace("__ERROR__",
                                    '<p class="err">Pick or type a valid network first.</p>'))
                return
            self._page(JOINING_PAGE.replace("__SSID__", html.escape(ssid)))
            threading.Thread(target=join_network,
                             args=(ssid, password, True), daemon=True).start()

        def log_message(self, fmt, *args):
            pass

    return Portal


def run_hotspot_mode():
    """Host the setup hotspot + portal until we're online again."""
    global _scanned
    _scanned = scan_networks()
    log(f"scanned {len(_scanned)} networks")
    if not start_hotspot():
        time.sleep(30)
        return
    write_state()
    _set_status(phase="hotspot")
    server = ThreadingHTTPServer(("0.0.0.0", 80), make_portal_handler())
    server.timeout = 5
    last_recheck = time.time()
    try:
        while True:
            server.handle_request()
            with _lock:
                phase = _status.get("phase")
            if phase == "connected":
                return
            # every couple of minutes, quietly check whether a known
            # network came back (e.g. the router finished rebooting)
            if phase == "hotspot" and saved_wifi_profiles() and \
                    time.time() - last_recheck > RECHECK_HOTSPOT:
                last_recheck = time.time()
                log("rechecking known networks...")
                stop_hotspot(delete=False)
                deadline = time.time() + 25
                while time.time() < deadline and not wifi_connected():
                    time.sleep(3)
                if wifi_connected():
                    log("known network is back")
                    stop_hotspot()
                    clear_state()
                    _set_status(phase="connected", error=None)
                    return
                start_hotspot()
                write_state()
    finally:
        server.server_close()


# ---------------------------------------------------- control API (:8079)

def make_control_handler():
    class Control(BaseHTTPRequestHandler):
        server_version = "HeadroomWifi/1.0"

        def _json(self, payload, code=200):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/networks":
                nets = scan_cached()
                self._json({
                    "current": current_ssid(),
                    "networks": [
                        {"ssid": s, "signal": sig, "secured": sec}
                        for s, sig, sec in nets
                    ],
                })
            elif path == "/status":
                with _lock:
                    payload = dict(_status)
                payload["current"] = current_ssid()
                self._json(payload)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/join":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                self._json({"ok": False, "error": "request too large"}, 413)
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                data = {}
            ssid = str(data.get("ssid", "")).strip()
            if not valid_ssid(ssid):
                self._json({"ok": False, "error": "invalid ssid"}, 400)
                return
            rescue = hotspot_active()
            threading.Thread(
                target=join_network,
                args=(ssid, str(data.get("password", "")), rescue),
                daemon=True,
            ).start()
            self._json({"ok": True})

        def log_message(self, fmt, *args):
            pass

    return Control


def main():
    clear_state()
    control = ThreadingHTTPServer(("127.0.0.1", CONTROL_PORT),
                                  make_control_handler())
    threading.Thread(target=control.serve_forever, daemon=True).start()
    log(f"control API on 127.0.0.1:{CONTROL_PORT}")

    down_since = time.time()
    while True:
        if wifi_connected():
            if _status.get("phase") != "joining":
                _set_status(phase="connected")
            down_since = None
        else:
            if down_since is None:
                down_since = time.time()
                log("network lost; waiting before starting setup hotspot")
            grace = FIRST_BOOT_GRACE if not saved_wifi_profiles() \
                else RECONNECT_GRACE
            with _lock:
                joining = _status.get("phase") == "joining"
            if not joining and not hotspot_active() and \
                    time.time() - down_since > grace:
                log("entering setup hotspot mode")
                run_hotspot_mode()
                down_since = None
        time.sleep(10)


if __name__ == "__main__":
    main()
