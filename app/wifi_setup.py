"""First-boot Wi-Fi provisioning: become a hotspot until we're online.

If the Pi boots and can't join any known network within a grace period,
this service (run as root by claude-tracker-wifi.service):

  1. scans for nearby networks (while the radio is still free),
  2. starts a WPA2 hotspot "ClaudeTracker-Setup" via NetworkManager,
  3. serves a tiny captive portal on http://10.42.0.1 where the user picks
     their home network and enters its password,
  4. joins that network; on success the hotspot is removed and we exit.
     On a wrong password the hotspot comes back with an error shown.

State is written to /run/claude-tracker/wifi.json so the HAT display can
show "join my setup Wi-Fi" with a QR code. Standard library + nmcli only
(NetworkManager is the default on Raspberry Pi OS Bookworm and later).
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

HOTSPOT_SSID = "ClaudeTracker-Setup"
HOTSPOT_PSK = "claudepi"          # printed on the device screen
HOTSPOT_CON = "ctp-hotspot"
PORTAL_IP = "10.42.0.1"           # NetworkManager's shared-mode gateway
GRACE_SECONDS = 45                # how long to wait for a known network
JOIN_TIMEOUT = 60

STATE_DIR = "/run/claude-tracker"
STATE_FILE = os.path.join(STATE_DIR, "wifi.json")

# Captive-portal probe paths used by phones/laptops; answering with a
# redirect makes the setup page pop up automatically after joining.
PROBE_PATHS = {
    "/generate_204", "/gen_204", "/hotspot-detect.html", "/ncsi.txt",
    "/connecttest.txt", "/success.txt", "/canonical.html",
    "/library/test/success.html",
}

_scanned = []      # [(ssid, signal, secured)] cached from the boot scan
_status = {"phase": "idle", "error": None, "target": None}
_status_lock = threading.Lock()


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def nmcli(*args, timeout=30):
    return subprocess.run(
        ["nmcli", *args], capture_output=True, text=True, timeout=timeout
    )


def wifi_connected():
    """True if any wifi/ethernet device is connected to a network."""
    r = nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device")
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[1] in ("wifi", "ethernet") \
                and parts[2].startswith("connected"):
            # our own hotspot also counts as "connected"; exclude it
            active = nmcli("-t", "-f", "NAME", "connection", "show",
                           "--active").stdout
            if HOTSPOT_CON in active:
                return False
            return True
    return False


def scan_networks():
    """Scan while the radio is free; returns [(ssid, signal, secured)]."""
    nmcli("device", "wifi", "rescan", timeout=20)
    time.sleep(4)
    r = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list")
    best = {}
    for line in r.stdout.splitlines():
        # SSIDs can contain escaped colons (\:)
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
    nmcli("connection", "delete", HOTSPOT_CON)  # stale profile, if any
    r = nmcli("device", "wifi", "hotspot", "ifname", "wlan0",
              "con-name", HOTSPOT_CON, "ssid", HOTSPOT_SSID,
              "password", HOTSPOT_PSK, timeout=45)
    if r.returncode != 0:
        log(f"hotspot failed: {r.stderr.strip()}")
        return False
    log(f"hotspot '{HOTSPOT_SSID}' up; portal at http://{PORTAL_IP}")
    return True


def join_network(ssid, password):
    """Try to join the user's network. Restores the hotspot on failure."""
    with _status_lock:
        _status.update(phase="joining", target=ssid, error=None)
    write_state(joining=ssid)
    nmcli("connection", "down", HOTSPOT_CON)
    args = ["device", "wifi", "connect", ssid, "ifname", "wlan0"]
    if password:
        args += ["password", password]
    r = nmcli(*args, timeout=JOIN_TIMEOUT)
    if r.returncode == 0 and wifi_connected():
        log(f"joined '{ssid}'")
        nmcli("connection", "delete", HOTSPOT_CON)
        clear_state()
        with _status_lock:
            _status.update(phase="done")
        return True
    error = (r.stderr or r.stdout).strip().splitlines()
    error = error[-1] if error else "unknown error"
    log(f"join '{ssid}' failed: {error}")
    # a failed profile would otherwise be retried forever by NM
    nmcli("connection", "delete", ssid)
    msg = (f"Couldn't join {ssid} — wrong password? The setup Wi-Fi is "
           "back; reconnect and try again.")
    with _status_lock:
        _status.update(phase="hotspot", error=msg)
    start_hotspot()
    write_state(error=msg)
    return False


PORTAL_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClaudeTracker Wi-Fi setup</title>
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
<p>The <b>ClaudeTracker-Setup</b> network will now disappear.</p>
<p><b>Watch the tracker's screen:</b> if it shows a QR code for
"Set me up", it's online — reconnect your phone to your own Wi-Fi and scan it.
If the setup network comes back instead, the password didn't work — rejoin it
and try again.</p></body></html>"""


def _network_rows():
    rows = []
    for ssid, signal, secured in _scanned:
        safe = html.escape(ssid, quote=True)
        lock = "🔒 " if secured else ""
        rows.append(
            f'<label class="net"><input type="radio" name="ssid" '
            f'value="{safe}">{lock}{html.escape(ssid)}'
            f'<span class="sig">{signal}%</span></label>'
        )
    return "\n".join(rows) or '<p class="muted">No networks found — type yours below.</p>'


def make_portal_handler():
    class Portal(BaseHTTPRequestHandler):
        server_version = "ClaudeTrackerSetup/1.0"

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
                # captive-portal probe or stray domain -> bounce to us
                self.send_response(302)
                self.send_header("Location", f"http://{PORTAL_IP}/")
                self.end_headers()
                return
            with _status_lock:
                error = _status.get("error")
            banner = f'<p class="err">{html.escape(error)}</p>' if error else ""
            self._page(PORTAL_PAGE.replace("__NETWORKS__", _network_rows())
                       .replace("__ERROR__", banner))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            form = urllib.parse.parse_qs(
                self.rfile.read(length).decode("utf-8", "replace"))
            ssid = (form.get("ssid_other", [""])[0].strip()
                    or form.get("ssid", [""])[0].strip())
            password = form.get("password", [""])[0]
            if not ssid:
                self._page(PORTAL_PAGE
                           .replace("__NETWORKS__", _network_rows())
                           .replace("__ERROR__",
                                    '<p class="err">Pick or type a network first.</p>'))
                return
            self._page(JOINING_PAGE.replace("__SSID__", html.escape(ssid)))
            threading.Thread(target=join_network, args=(ssid, password),
                             daemon=True).start()

        def log_message(self, fmt, *args):
            pass

    return Portal


def main():
    if os.environ.get("CTP_WIFI_FAKE"):  # local dev: portal UI only
        global _scanned
        _scanned = [("HomeWiFi", 92, True), ("Neighbor5G", 61, True),
                    ("CafeOpen", 40, False)]
    else:
        deadline = time.time() + GRACE_SECONDS
        while time.time() < deadline:
            if wifi_connected():
                log("network present; provisioning not needed")
                clear_state()
                return
            time.sleep(3)
        log("no network after grace period; entering setup mode")
        _scanned[:] = scan_networks()
        log(f"scanned {len(_scanned)} networks")
        if not start_hotspot():
            return
        write_state()
        with _status_lock:
            _status.update(phase="hotspot")

    server = ThreadingHTTPServer(("0.0.0.0", 80), make_portal_handler())
    server.timeout = 5  # wake periodically so we notice a finished join
    log("portal listening on :80")
    while True:
        server.handle_request()
        with _status_lock:
            if _status.get("phase") == "done":
                break
    log("provisioning complete")


if __name__ == "__main__":
    main()
