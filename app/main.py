"""ClaudeTrackerPi -- tiny web dashboard for Claude usage limits + PiSugar battery.

Run:  python3 app/main.py            (real data, needs credentials -- see README)
      python3 app/main.py --demo     (fake data, no credentials needed)

Standard library only, sized for a Pi Zero 2 W.
"""

import argparse
import datetime
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic_usage
import display
import notify
import pisugar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "app", "web")

DEFAULT_CONFIG = {
    "port": 8080,
    "usage_poll_seconds": 120,
    "battery_poll_seconds": 20,
    "credentials_path": None,
    "night_start": "22:00",   # when the mascot goes to sleep
    "night_end": "07:00",     # and when it wakes up
    "hat_display": True,      # draw on the Whisplay HAT LCD when present
    # "push" (default): a companion app on your computer computes usage from
    # Claude Code's local logs and POSTs it here. "oauth": legacy direct-to-
    # Anthropic path (no longer works -- Anthropic blocks third-party OAuth).
    "data_source": "push",
    "push_token": "",         # optional shared secret the companion must send
    "theme": "auto",          # "auto" (day/night by schedule), "light", "dark"
    "clock_24h": False,       # 24-hour time instead of AM/PM
    "meter_mode": "left",     # "left" (usage remaining) or "used"
    "brightness": 100,        # LCD backlight 0-100 (dims with PWM; on/off else)
    "night_dim": True,        # dim the LCD during the night window
    "lcd_history": True,      # rotate a usage-history graph onto the LCD
    "qr_interval": 60,        # seconds between phone-QR appearances (0 = off)
    "audio_alerts": False,    # beep on out-of-credits / restored (off default)
    "push_service": "none",   # "none" | "ntfy" | "pushover"
    "ntfy_topic": "",         # your ntfy.sh topic
    "pushover_token": "",     # your own Pushover app token
    "pushover_user": "",      # your Pushover user key
}

# How long after the last observed usage increase we still call the
# session "active" (Pip keeps dancing between messages).
ACTIVE_SECONDS = 900

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def config_path():
    return os.environ.get(
        "CLAUDE_TRACKER_CONFIG", os.path.join(ROOT, "config.json")
    )


def load_config():
    path = config_path()
    config = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                config.update(json.load(fh))
        except (OSError, ValueError) as exc:
            print(f"Warning: ignoring bad config {path}: {exc}", file=sys.stderr)
    return config


def save_config(config):
    """Persist the full effective config so settings survive a restart."""
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    os.replace(tmp, path)


# Usage history: a small per-window ring buffer of [epoch, utilization] points,
# persisted so the trend survives a restart. At ~2-min pushes, 180 points is
# roughly the last 6 hours.
HISTORY_CAP = 180
HISTORY_MIN_GAP = 90          # seconds; coalesce points closer than this


def history_path():
    return os.path.join(os.path.dirname(config_path()) or ".", "history.json")


def load_history():
    try:
        with open(history_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): [p for p in v
                             if isinstance(p, list) and len(p) == 2]
                    for k, v in data.items()}
    except (OSError, ValueError):
        pass
    return {}


def save_history(history):
    try:
        path = history_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(history, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def record_history(state, windows, now):
    """Append the current utilization of each window to its series (capped)."""
    for w in windows:
        key = w.get("key")
        if not key:
            continue
        series = state.history.setdefault(key, [])
        point = [now, round(float(w["utilization"]), 1)]
        if series and now - series[-1][0] < HISTORY_MIN_GAP:
            series[-1] = point          # coalesce rapid pushes into one point
        else:
            series.append(point)
        if len(series) > HISTORY_CAP:
            del series[:-HISTORY_CAP]


# Settings the /settings page may change, each with a validator/normalizer.
def _clamp_brightness(v):
    try:
        return max(10, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _clamp_qr(v):
    try:
        return max(0, min(3600, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


# Semi-secret fields: masked in GET, and the mask is ignored on POST so
# leaving the field untouched doesn't overwrite the saved value.
SECRET_FIELDS = ("pushover_token", "pushover_user")
SECRET_MASK = "••••••"

SETTINGS_FIELDS = {
    "theme": lambda v: v if v in ("auto", "light", "dark") else None,
    "clock_24h": lambda v: bool(v),
    "meter_mode": lambda v: v if v in ("left", "used") else None,
    "brightness": _clamp_brightness,
    "night_dim": lambda v: bool(v),
    "lcd_history": lambda v: bool(v),
    "qr_interval": _clamp_qr,
    "audio_alerts": lambda v: bool(v),
    "push_service": lambda v: v if v in ("none", "ntfy", "pushover") else None,
    "ntfy_topic": lambda v: str(v)[:80],
    "pushover_token": lambda v: str(v)[:64],
    "pushover_user": lambda v: str(v)[:64],
}


def apply_settings(state, payload):
    """Validate + persist settings from the web form. Returns the applied dict."""
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    applied = {}
    for key, normalize in SETTINGS_FIELDS.items():
        if key not in payload:
            continue
        if key in SECRET_FIELDS and payload[key] == SECRET_MASK:
            continue          # unchanged mask -> keep the saved value
        value = normalize(payload[key])
        if value is None:
            raise ValueError(f"invalid value for {key!r}")
        applied[key] = value
    with state.lock:
        state.config.update(applied)   # same dict the display + snapshot read
    save_config(state.config)
    return applied


class State:
    """Cached data the poller writes and the HTTP handler reads."""

    def __init__(self, config=None):
        self.config = config or DEFAULT_CONFIG
        self.lock = threading.Lock()
        self.usage = None          # {"windows": [...], "plan": ...}
        self.usage_error = None
        self.usage_updated = None  # epoch seconds
        self.battery = None
        self.battery_present = True
        self.prev_utilization = None  # {window key: utilization} from last poll
        self.last_activity = 0.0      # when utilization last went up
        self.wifi = {"ssid": None, "ip": None}  # current network
        self.history = load_history()  # {key: [[epoch, utilization], ...]}
        self.session_maxed = None      # last known "out of session usage" state

    def snapshot(self):
        with self.lock:
            return {
                "app": "ClaudeTrackerPi",
                "windows": (self.usage or {}).get("windows", []),
                "plan": (self.usage or {}).get("plan"),
                "usage_error": self.usage_error,
                "usage_updated": self.usage_updated,
                "battery": self.battery if self.battery_present else None,
                "wifi": dict(self.wifi),
                "session_active": bool(
                    self.last_activity
                    and time.time() - self.last_activity < ACTIVE_SECONDS
                ),
                "night": {
                    "start": self.config.get("night_start", "22:00"),
                    "end": self.config.get("night_end", "07:00"),
                },
                "theme": self.config.get("theme", "auto"),
                "clock_24h": bool(self.config.get("clock_24h", False)),
                "meter_mode": self.config.get("meter_mode", "left"),
                "brightness": self.config.get("brightness", 100),
                "night_dim": bool(self.config.get("night_dim", True)),
                "lcd_history": bool(self.config.get("lcd_history", True)),
                "qr_interval": int(self.config.get("qr_interval", 60)),
                "history": {k: list(v) for k, v in self.history.items()},
                "server_time": time.time(),
            }


def demo_snapshot():
    """Fake but lively data so the dashboard can be previewed anywhere."""
    now = datetime.datetime.now(datetime.timezone.utc)
    t = time.time()
    session = 38 + 25 * abs(math.sin(t / 90))
    weekly = 62.0
    opus = 91.0
    session_reset = now + datetime.timedelta(hours=2, minutes=14)
    weekly_reset = now + datetime.timedelta(days=3, hours=5)
    return {
        "app": "ClaudeTrackerPi",
        "windows": [
            {"key": "five_hour", "label": "Session (5 hour)",
             "utilization": round(session, 1),
             "resets_at": session_reset.isoformat()},
            {"key": "seven_day", "label": "Weekly (all models)",
             "utilization": weekly, "resets_at": weekly_reset.isoformat()},
            {"key": "seven_day_opus", "label": "Weekly (Opus)",
             "utilization": opus, "resets_at": weekly_reset.isoformat()},
        ],
        "plan": "max",
        "usage_error": None,
        "usage_updated": t,
        "battery": {"percent": 76.0, "charging": False, "plugged": False},
        "wifi": {"ssid": "HomeWiFi"},
        "session_active": True,
        "night": {"start": "22:00", "end": "07:00"},
        "theme": "auto",
        "clock_24h": False,
        "meter_mode": "left",
        "brightness": 100,
        "night_dim": True,
        "lcd_history": True,
        "qr_interval": 60,
        "history": _demo_history(t, session, weekly, opus),
        "server_time": t,
    }


def _demo_history(t, session, weekly, opus):
    """Synthetic ~5h of history so the graph/sparklines preview with data."""
    n = 80
    pts = {"five_hour": [], "seven_day": [], "seven_day_opus": []}
    for i in range(n):
        ago = (n - 1 - i) * 180          # 3-min spacing
        ts = t - ago
        f = i / (n - 1)
        pts["five_hour"].append([ts, round(session * (0.15 + 0.85 * f) +
                                            3 * math.sin(i / 4), 1)])
        pts["seven_day"].append([ts, round(weekly * (0.7 + 0.3 * f), 1)])
        pts["seven_day_opus"].append([ts, round(opus * (0.8 + 0.2 * f), 1)])
    return pts


def start_pollers(state, config):
    creds_configured = config.get("credentials_path")

    def wait_for_push():
        # In push mode the companion app feeds us data via POST /api/push; we
        # just show a helpful hint until the first push lands.
        while True:
            with state.lock:
                stale = (state.usage_updated is None
                         or time.time() - state.usage_updated > 600)
                if stale:
                    state.usage_error = (
                        "Waiting for the ClaudeTracker companion on your "
                        "computer. See the setup guide to start it."
                    )
            time.sleep(15)

    def poll_usage():
        while True:
            path = anthropic_usage.find_credentials_path(creds_configured)
            if path is None:
                message = (
                    "No Claude credentials on this device. Use the companion "
                    "app instead — see the setup guide."
                )
                with state.lock:
                    state.usage_error = message
            else:
                try:
                    usage = anthropic_usage.fetch_usage(path)
                    now_util = {
                        w["key"]: w["utilization"] for w in usage["windows"]
                    }
                    with state.lock:
                        prev = state.prev_utilization
                        if prev is not None and any(
                            prev.get(key) is None or value > prev[key] + 0.01
                            for key, value in now_util.items()
                        ):
                            # usage went up (or a new window appeared) since
                            # the last poll -> someone is talking to Claude
                            state.last_activity = time.time()
                        state.prev_utilization = now_util
                        state.usage = usage
                        state.usage_error = None
                        state.usage_updated = time.time()
                except anthropic_usage.UsageError as exc:
                    with state.lock:
                        state.usage_error = str(exc)
                except Exception as exc:  # never let the poller die
                    with state.lock:
                        state.usage_error = f"Unexpected error: {exc}"
            time.sleep(max(30, int(config["usage_poll_seconds"])))

    def poll_battery():
        misses = 0
        while True:
            battery = pisugar.read_battery()
            with state.lock:
                state.battery = battery
                if battery is None:
                    misses += 1
                    # After a few misses assume no PiSugar; keep checking
                    # occasionally in case pisugar-server starts later.
                    state.battery_present = misses < 3
                else:
                    misses = 0
                    state.battery_present = True
            time.sleep(max(5, int(config["battery_poll_seconds"])))

    def poll_wifi():
        while True:
            try:
                ssid = _wifi_api("/status").get("current")
            except (urllib.error.URLError, OSError, ValueError):
                ssid = None
            with state.lock:
                state.wifi = {"ssid": ssid}
            time.sleep(15)

    usage_worker = poll_usage if config.get("data_source") == "oauth" \
        else wait_for_push
    for target in (usage_worker, poll_battery, poll_wifi):
        threading.Thread(target=target, daemon=True).start()


def _creds_write_path(config):
    configured = (config or {}).get("credentials_path")
    if configured:
        return os.path.expanduser(configured)
    return os.path.expanduser("~/.claude-tracker/credentials.json")


def apply_push(state, config, payload):
    """Store usage pushed by the companion app. Returns None or raises ValueError.

    payload = {"windows": [{"key","label","utilization","resets_at"}, ...],
               "plan": str|None, "active": bool (optional)}
    """
    windows = payload.get("windows")
    if not isinstance(windows, list):
        raise ValueError("missing 'windows' list")
    clean = []
    for w in windows:
        if not isinstance(w, dict) or "utilization" not in w:
            continue
        try:
            util = max(0.0, min(100.0, float(w["utilization"])))
        except (TypeError, ValueError):
            continue
        clean.append({
            "key": str(w.get("key", "")),
            "label": str(w.get("label", w.get("key", "Usage"))),
            "utilization": util,
            "resets_at": w.get("resets_at"),
        })
    now_util = {w["key"]: w["utilization"] for w in clean}
    with state.lock:
        prev = state.prev_utilization
        went_up = prev is not None and any(
            prev.get(k) is None or v > prev[k] + 0.01 for k, v in now_util.items()
        )
        if payload.get("active") or went_up:
            state.last_activity = time.time()
        state.prev_utilization = now_util
        state.usage = {"windows": clean, "plan": payload.get("plan")}
        state.usage_error = None
        state.usage_updated = time.time()
        record_history(state, clean, time.time())
        history_copy = {k: list(v) for k, v in state.history.items()}
        # out-of-credits / restored transition (5-hour session window)
        session = next((w["utilization"] for w in clean
                        if w["key"] == "five_hour"), None)
        transition = None
        if session is not None:
            maxed = session >= 99.5
            if state.session_maxed is not None and maxed != state.session_maxed:
                transition = "out" if maxed else "restored"
            state.session_maxed = maxed
    save_history(history_copy)   # persist outside the lock (file I/O)
    if transition:
        notify.alert(state.config, transition)


WIFI_API = "http://127.0.0.1:8079"   # wifi_setup.py's localhost control API

DEMO_NETWORKS = {
    "current": "HomeWiFi",
    "networks": [
        {"ssid": "HomeWiFi", "signal": 92, "secured": True},
        {"ssid": "Neighbor5G", "signal": 58, "secured": True},
        {"ssid": "CoffeeShop", "signal": 34, "secured": False},
    ],
}


def _wifi_api(path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(WIFI_API + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


WIFI_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wi-Fi settings</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 18px 40px; font-family:system-ui,-apple-system,
    "Segoe UI",sans-serif; background:#f0eee6; color:#3d3929; line-height:1.5; }
  @media (prefers-color-scheme: dark) {
    body { background:#262624; color:#f5f4ef; }
    .card { background:#30302e !important; border-color:rgba(245,244,239,.1) !important; }
    input { background:#1a1a19 !important; color:#f5f4ef !important;
      border-color:rgba(245,244,239,.2) !important; }
    .muted { color:#94907e !important; }
    label.net { border-color:rgba(245,244,239,.08) !important; }
  }
  .wrap { max-width:460px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .muted { color:#8a8478; font-size:.9rem; }
  .card { background:#faf9f5; border:1px solid rgba(61,57,41,.12);
    border-radius:14px; padding:16px; margin-top:16px; }
  label.net { display:flex; align-items:center; gap:10px; padding:10px 6px;
    border-bottom:1px solid rgba(61,57,41,.08); font-size:1rem; }
  label.net:last-of-type { border-bottom:none; }
  .sig { margin-left:auto; color:#8a8478; font-size:.8rem; }
  .cur { color:#0f7b3f; font-size:.8rem; font-weight:600; }
  input[type=text],input[type=password] { width:100%; padding:12px; font-size:1rem;
    border-radius:10px; border:1px solid rgba(61,57,41,.25); background:#fff;
    margin:6px 0 12px; }
  .btn { display:block; width:100%; text-align:center; background:#d97757;
    color:#fff; font-weight:600; font-size:1.05rem; padding:14px;
    border-radius:10px; border:none; cursor:pointer; }
  .btn[disabled] { opacity:.6; }
  #msg { margin-top:14px; font-size:.95rem; }
  .ok { color:#0f7b3f; font-weight:600; }
  .err { color:#c4453d; font-weight:600; }
  a.back { color:#c15f3c; }
</style></head>
<body><div class="wrap">
  <h1>Wi-Fi settings</h1>
  <p class="muted" id="cur">Checking current network…</p>
  <div class="card">
    <div id="nets"><p class="muted">Scanning for networks…</p></div>
    <div style="margin-top:12px">
      <input type="text" id="ssid_other" placeholder="Or type a network name">
      <input type="password" id="password" placeholder="Wi-Fi password">
      <button class="btn" id="go" onclick="join()">Connect</button>
    </div>
    <div id="msg"></div>
  </div>
  <p class="muted" style="margin-top:14px">Switching drops the tracker off
    the network for ~30 seconds. If it can't join the new network, it comes
    back on the old one (or starts its <b>ClaudeTracker-Setup</b> hotspot).</p>
  <p class="muted"><a class="back" href="/">&larr; Back to dashboard</a></p>
</div>
<script>
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
async function load(){
  try{
    var r=await fetch('/api/wifi/networks'); var d=await r.json();
    if(d.error){ document.getElementById('nets').innerHTML=
      '<p class="err">'+esc(d.error)+'</p>'; return; }
    document.getElementById('cur').textContent =
      d.current ? ('Connected to: '+d.current) : 'Not connected to Wi-Fi.';
    var rows = d.networks.map(function(n){
      var cur = (n.ssid===d.current) ? ' <span class="cur">current</span>' : '';
      return '<label class="net"><input type="radio" name="ssid" value="'+
        esc(n.ssid)+'">'+(n.secured?'&#128274; ':'')+esc(n.ssid)+cur+
        '<span class="sig">'+n.signal+'%</span></label>';
    }).join('');
    document.getElementById('nets').innerHTML =
      rows || '<p class="muted">No networks found.</p>';
  }catch(e){
    document.getElementById('nets').innerHTML =
      '<p class="err">Wi-Fi service unavailable on this device.</p>';
  }
}
async function join(){
  var msg=document.getElementById('msg'), go=document.getElementById('go');
  var sel=document.querySelector('input[name=ssid]:checked');
  var ssid=(document.getElementById('ssid_other').value.trim())||(sel&&sel.value)||'';
  if(!ssid){ msg.innerHTML='<span class="err">Pick or type a network first.</span>'; return; }
  go.disabled=true;
  msg.innerHTML='Switching to <b>'+esc(ssid)+'</b>… the tracker may go quiet for ~30s.';
  try{
    var r=await fetch('/api/wifi/join',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ssid:ssid,password:document.getElementById('password').value})});
    var d=await r.json();
    if(!d.ok){ go.disabled=false;
      msg.innerHTML='<span class="err">'+esc(d.error||'Failed.')+'</span>'; return; }
  }catch(e){ /* expected if the network drops mid-switch */ }
  var tries=0;
  var poll=setInterval(async function(){
    tries++;
    try{
      var r=await fetch('/api/wifi/status'); var s=await r.json();
      if(s.error && s.phase!=='joining'){ clearInterval(poll); go.disabled=false;
        msg.innerHTML='<span class="err">'+esc(s.error)+'</span>'; load(); return; }
      if(s.phase==='connected' && s.current){ clearInterval(poll);
        msg.innerHTML='<span class="ok">Connected to '+esc(s.current)+'.</span>';
        go.disabled=false; load(); return; }
    }catch(e){ /* device switching networks */ }
    if(tries>30){ clearInterval(poll); go.disabled=false;
      msg.innerHTML='If this page stopped updating, the tracker moved '+
        'networks — reopen the dashboard from its screen QR.'; }
  }, 3000);
}
load();
</script>
</body></html>"""


SETTINGS_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings</title>
<style>
  :root { color-scheme: light dark; --accent:#d97757; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 18px 40px; font-family:system-ui,-apple-system,
    "Segoe UI",sans-serif; background:#f0eee6; color:#3d3929; line-height:1.5; }
  @media (prefers-color-scheme: dark) {
    body { background:#262624; color:#f5f4ef; }
    .card { background:#30302e !important; border-color:rgba(245,244,239,.1) !important; }
    .muted { color:#94907e !important; }
    select { background:#1a1a19 !important; color:#f5f4ef !important;
      border-color:rgba(245,244,239,.2) !important; }
    .row { border-color:rgba(245,244,239,.08) !important; }
  }
  .wrap { max-width:460px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .muted { color:#8a8478; font-size:.9rem; }
  .card { background:#faf9f5; border:1px solid rgba(61,57,41,.12);
    border-radius:14px; padding:4px 16px; margin-top:16px; }
  .row { display:flex; align-items:center; gap:12px; padding:14px 0;
    border-bottom:1px solid rgba(61,57,41,.08); }
  .row:last-child { border-bottom:none; }
  .row .lab { flex:1; }
  .row .lab small { display:block; color:#8a8478; font-size:.82rem; }
  select, input[type=text] { font-size:1rem; padding:8px 10px; border-radius:9px;
    border:1px solid rgba(61,57,41,.25); background:#fff; color:#3d3929; }
  input[type=text] { width:100%; margin-top:6px; }
  @media (prefers-color-scheme: dark) {
    input[type=text] { background:#1a1a19 !important; color:#f5f4ef !important;
      border-color:rgba(245,244,239,.2) !important; }
  }
  .sub { display:none; padding:2px 0 12px; }
  .sub.on { display:block; }
  .sub label { display:block; font-size:.82rem; color:#8a8478; margin-top:8px; }
  h2 { font-size:1rem; margin:14px 2px 2px; color:#8a8478; font-weight:600; }
  .sw { position:relative; width:46px; height:28px; flex:none; }
  .sw input { opacity:0; width:0; height:0; }
  .sw span { position:absolute; inset:0; background:#cfcabb; border-radius:999px;
    transition:.2s; cursor:pointer; }
  .sw span::before { content:""; position:absolute; height:22px; width:22px;
    left:3px; top:3px; background:#fff; border-radius:50%; transition:.2s; }
  .sw input:checked + span { background:var(--accent); }
  .sw input:checked + span::before { transform:translateX(18px); }
  .tbtn { font-size:.95rem; font-weight:600; padding:9px 15px; border-radius:9px;
    flex:none; border:1px solid var(--accent); background:transparent;
    color:var(--accent); cursor:pointer; }
  .tbtn:disabled { opacity:.5; cursor:default; }
  .status { margin-top:14px; font-size:.9rem; min-height:1.2em; }
  .ok { color:#0f7b3f; } .err { color:#c4453d; }
  a.back { display:inline-block; margin-top:22px; color:var(--accent);
    text-decoration:none; font-weight:600; }
</style>
</head><body>
<div class="wrap">
  <h1>Settings</h1>
  <p class="muted">Changes save instantly and apply to the screen and this
    dashboard.</p>

  <div class="card">
    <div class="row">
      <div class="lab">Appearance<small>Cream by day, dark at night — or force one</small></div>
      <select id="theme">
        <option value="auto">Auto (day / night)</option>
        <option value="light">Always light</option>
        <option value="dark">Always dark</option>
      </select>
    </div>
    <div class="row">
      <div class="lab">Meters show<small>How much is left, or how much is used</small></div>
      <select id="meter_mode">
        <option value="left">% left</option>
        <option value="used">% used</option>
      </select>
    </div>
    <div class="row">
      <div class="lab">24-hour clock<small>Show 18:30 instead of 6:30 PM</small></div>
      <label class="sw"><input type="checkbox" id="clock_24h"><span></span></label>
    </div>
    <div class="row">
      <div class="lab">Brightness<small>Screen backlight (dims on HATs with PWM)</small></div>
      <span id="brightval" class="muted" style="min-width:40px;text-align:right">100%</span>
      <input type="range" id="brightness" min="10" max="100" step="5" style="flex:none;width:110px">
    </div>
    <div class="row">
      <div class="lab">Dim at night<small>Lower the brightness during the night window</small></div>
      <label class="sw"><input type="checkbox" id="night_dim"><span></span></label>
    </div>
    <div class="row">
      <div class="lab">Usage history on screen<small>Rotate a trend graph onto the LCD</small></div>
      <label class="sw"><input type="checkbox" id="lcd_history"><span></span></label>
    </div>
    <div class="row">
      <div class="lab">Phone QR on screen<small>How often the scan-me QR appears</small></div>
      <select id="qr_interval">
        <option value="0">Off</option>
        <option value="60">Every ~1 min</option>
        <option value="120">Every ~2 min</option>
        <option value="300">Every ~5 min</option>
      </select>
    </div>
  </div>

  <h2>Alerts</h2>
  <div class="card">
    <div class="row">
      <div class="lab">Audio alert<small>Beep from the speaker when you run out / recover</small></div>
      <label class="sw"><input type="checkbox" id="audio_alerts"><span></span></label>
    </div>
    <div class="row">
      <div class="lab">Phone notifications<small>Ping your phone when you run out / recover</small></div>
      <select id="push_service">
        <option value="none">Off</option>
        <option value="ntfy">ntfy (free, no account)</option>
        <option value="pushover">Pushover (your own keys)</option>
      </select>
    </div>
    <div class="sub" id="sub-ntfy">
      <label>ntfy topic — pick a hard-to-guess name, then subscribe to it in the ntfy app</label>
      <input type="text" id="ntfy_topic" placeholder="e.g. claude-dave-7fq2" autocomplete="off">
    </div>
    <div class="sub" id="sub-pushover">
      <label>Pushover application token (register an app at pushover.net)</label>
      <input type="text" id="pushover_token" placeholder="a1b2c3…" autocomplete="off">
      <label>Pushover user key</label>
      <input type="text" id="pushover_user" placeholder="u1v2w3…" autocomplete="off">
    </div>
    <div class="row" style="border-bottom:none">
      <div class="lab">Test it<small>Fire a sample alert to check your setup</small></div>
      <button id="testbtn" class="tbtn">Send test</button>
    </div>
  </div>

  <div class="status" id="status"></div>
  <a class="back" href="/">← Back to the dashboard</a>
</div>

<script>
  var status = document.getElementById("status");
  function flash(msg, ok) {
    status.className = "status " + (ok ? "ok" : "err");
    status.textContent = msg;
    if (ok) setTimeout(function(){ status.textContent = ""; }, 1500);
  }
  function syncSubs() {
    var svc = document.getElementById("push_service").value;
    document.getElementById("sub-ntfy").classList.toggle("on", svc === "ntfy");
    document.getElementById("sub-pushover").classList.toggle("on", svc === "pushover");
  }
  async function load() {
    try {
      var s = await (await fetch("/api/settings")).json();
      document.getElementById("theme").value = s.theme || "auto";
      document.getElementById("meter_mode").value = s.meter_mode || "left";
      document.getElementById("clock_24h").checked = !!s.clock_24h;
      document.getElementById("brightness").value = s.brightness || 100;
      document.getElementById("brightval").textContent = (s.brightness || 100) + "%";
      document.getElementById("night_dim").checked = !!s.night_dim;
      document.getElementById("lcd_history").checked = !!s.lcd_history;
      document.getElementById("qr_interval").value = String(s.qr_interval != null ? s.qr_interval : 60);
      document.getElementById("audio_alerts").checked = !!s.audio_alerts;
      document.getElementById("push_service").value = s.push_service || "none";
      document.getElementById("ntfy_topic").value = s.ntfy_topic || "";
      document.getElementById("pushover_token").value = s.pushover_token || "";
      document.getElementById("pushover_user").value = s.pushover_user || "";
      syncSubs();
    } catch (e) { flash("Couldn't load settings.", false); }
  }
  async function save(patch) {
    try {
      var r = await fetch("/api/settings", { method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify(patch) });
      var d = await r.json();
      if (d.ok) flash("Saved.", true); else flash(d.error || "Couldn't save.", false);
    } catch (e) { flash("Couldn't reach the tracker.", false); }
  }
  document.getElementById("theme").addEventListener("change", function(e){
    save({ theme: e.target.value });
  });
  document.getElementById("meter_mode").addEventListener("change", function(e){
    save({ meter_mode: e.target.value });
  });
  document.getElementById("clock_24h").addEventListener("change", function(e){
    save({ clock_24h: e.target.checked });
  });
  document.getElementById("qr_interval").addEventListener("change", function(e){
    save({ qr_interval: parseInt(e.target.value, 10) });
  });
  document.getElementById("brightness").addEventListener("input", function(e){
    document.getElementById("brightval").textContent = e.target.value + "%";
  });
  document.getElementById("brightness").addEventListener("change", function(e){
    save({ brightness: parseInt(e.target.value, 10) });
  });
  document.getElementById("testbtn").addEventListener("click", async function(e){
    var b = e.target; b.disabled = true;
    status.className = "status"; status.textContent = "Sending test…";
    try {
      var d = await (await fetch("/api/test-alert", { method:"POST" })).json();
      var ok = /sent|playing/.test(d.push + d.audio);
      status.className = "status " + (ok ? "ok" : "err");
      status.textContent = "Test — speaker: " + d.audio + " · phone: " + d.push;
    } catch (err) { flash("Test failed to run.", false); }
    b.disabled = false;
  });
  document.getElementById("night_dim").addEventListener("change", function(e){
    save({ night_dim: e.target.checked });
  });
  document.getElementById("lcd_history").addEventListener("change", function(e){
    save({ lcd_history: e.target.checked });
  });
  document.getElementById("audio_alerts").addEventListener("change", function(e){
    save({ audio_alerts: e.target.checked });
  });
  document.getElementById("push_service").addEventListener("change", function(e){
    syncSubs(); save({ push_service: e.target.value });
  });
  ["ntfy_topic","pushover_token","pushover_user"].forEach(function(id){
    document.getElementById(id).addEventListener("change", function(e){
      var p = {}; p[id] = e.target.value.trim(); save(p);
    });
  });
  load();
</script>
</body></html>"""


def make_handler(state, demo):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudeTrackerPi/1.0"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                snapshot = demo_snapshot() if demo else state.snapshot()
                self._send_json(snapshot)
                return
            if path == "/setup":
                self._send_file("setup.html")
                return
            if path == "/wifi":
                self._send_html(WIFI_PAGE)
                return
            if path == "/settings":
                self._send_html(SETTINGS_PAGE)
                return
            if path == "/api/settings":
                out = {}
                for k in SETTINGS_FIELDS:
                    v = state.config.get(k)
                    if k in SECRET_FIELDS and v:
                        v = SECRET_MASK          # don't leak saved secrets
                    out[k] = v
                self._send_json(out)
                return
            if path == "/api/wifi/networks":
                if demo:
                    self._send_json(DEMO_NETWORKS)
                    return
                try:
                    self._send_json(_wifi_api("/networks"))
                except (urllib.error.URLError, OSError, ValueError):
                    self._send_json({"error": "Wi-Fi service isn't running "
                                     "on this device."})
                return
            if path == "/api/wifi/status":
                if demo:
                    self._send_json({"phase": "connected",
                                     "current": "HomeWiFi"})
                    return
                try:
                    self._send_json(_wifi_api("/status"))
                except (urllib.error.URLError, OSError, ValueError):
                    self._send_json({"error": "Wi-Fi service isn't running "
                                     "on this device."})
                return
            if path == "/":
                path = "/index.html"
            self._send_file(path.lstrip("/"))

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/push":
                self._handle_push()
                return
            if path == "/api/settings":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    applied = apply_settings(state, payload)
                    self._send_json({"ok": True, "applied": applied})
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, code=400)
                return
            if path == "/api/test-alert":
                result = notify.send_test(state.config)
                self._send_json({"ok": True, **result})
                return
            if path == "/api/wifi/join":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                if demo:
                    self._send_json({"ok": False,
                                     "error": "Disabled in demo mode."})
                    return
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                    self._send_json(_wifi_api("/join", payload))
                except (urllib.error.URLError, OSError, ValueError):
                    self._send_json({"ok": False, "error": "Wi-Fi service "
                                     "isn't running on this device."})
                return
            self.send_error(404)

        def _send_html(self, text):
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_push(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            token = state.config.get("push_token") or ""
            if token and self.headers.get("X-Push-Token") != token:
                self._send_json({"ok": False, "error": "bad push token"}, code=403)
                return
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                apply_push(state, state.config, payload)
                self._send_json({"ok": True})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, code=400)
            except Exception as exc:
                self._send_json({"ok": False, "error": f"error: {exc}"}, code=500)

        def _send_json(self, payload, code=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, relpath):
            full = os.path.realpath(os.path.join(WEB_DIR, relpath))
            if not full.startswith(os.path.realpath(WEB_DIR) + os.sep):
                self.send_error(403)
                return
            if not os.path.isfile(full):
                self.send_error(404)
                return
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header(
                "Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # keep the journal quiet on the Pi

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Claude usage tracker for Pi")
    parser.add_argument("--demo", action="store_true",
                        help="serve fake data (no credentials needed)")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    config = load_config()
    if args.port:
        config["port"] = args.port

    state = State(config)
    if not args.demo:
        start_pollers(state, config)

    if config.get("hat_display", True):
        snapshot_fn = demo_snapshot if args.demo else state.snapshot
        threading.Thread(
            target=display.run, args=(snapshot_fn, config), daemon=True
        ).start()

    server = ThreadingHTTPServer(
        ("0.0.0.0", int(config["port"])), make_handler(state, args.demo)
    )
    mode = "DEMO data" if args.demo else "live data"
    print(f"ClaudeTrackerPi serving {mode} on http://0.0.0.0:{config['port']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
