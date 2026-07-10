"""ClaudeTrackerPi -- tiny web dashboard for Claude usage limits + PiSugar battery.

Run:  python3 app/main.py            (real data, needs credentials -- see README)
      python3 app/main.py --demo     (fake data, no credentials needed)

Standard library only, sized for a Pi Zero 2 W.
"""

import argparse
import datetime
import html
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
import oauth_login
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


def load_config():
    path = os.environ.get(
        "CLAUDE_TRACKER_CONFIG", os.path.join(ROOT, "config.json")
    )
    config = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                config.update(json.load(fh))
        except (OSError, ValueError) as exc:
            print(f"Warning: ignoring bad config {path}: {exc}", file=sys.stderr)
    return config


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
        self.pending_verifier = None  # PKCE verifier during phone sign-in
        self.wifi = {"ssid": None, "ip": None}  # current network

    def snapshot(self):
        with self.lock:
            return {
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
        "server_time": t,
    }


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
                    "Not connected to Claude yet — open this dashboard and "
                    "tap 'Sign in with Claude'."
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


def complete_login(state, code, verifier=None):
    """Exchange a pasted code, save credentials, and refresh usage now.

    Returns the plan name on success. Raises oauth_login.LoginError on
    anything the user can fix (bad/expired code, expired session). The
    verifier is sent back by the page (robust to mobile tab reloads); we
    fall back to the server-held one for older page loads.
    """
    verifier = verifier or state.pending_verifier
    if not verifier:
        raise oauth_login.LoginError(
            "This sign-in link expired. Reload the page and try again."
        )
    oauth = oauth_login.exchange_code(code, verifier)
    path = _creds_write_path(state.config)
    oauth_login.save_credentials(oauth, path)
    with state.lock:
        state.pending_verifier = None
    try:
        usage = anthropic_usage.fetch_usage(path)
        with state.lock:
            state.usage = usage
            state.usage_error = None
            state.usage_updated = time.time()
            state.prev_utilization = {
                w["key"]: w["utilization"] for w in usage["windows"]
            }
        return (usage or {}).get("plan")
    except anthropic_usage.UsageError as exc:
        # Signed in fine, but the first usage read didn't land; the poller
        # will retry shortly. Don't treat this as a sign-in failure.
        with state.lock:
            state.usage_error = str(exc)
        return None


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


def complete_paste(state, text):
    """Save credentials pasted from an existing Claude Code login.

    Accepts the whole ~/.claude/.credentials.json ({"claudeAiOauth": {...}})
    or just the inner object. Bypasses the OAuth exchange entirely, so it
    works even when the sign-in endpoint is rate-limited. Returns the plan.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise oauth_login.LoginError("That isn't valid JSON. Paste the whole "
                                     "contents of the credentials file.")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if oauth is None and isinstance(data, dict) and data.get("accessToken"):
        oauth = data
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise oauth_login.LoginError("Couldn't find an access token in that "
                                     "text. Copy the full credentials file.")
    path = _creds_write_path(state.config)
    oauth_login.save_credentials(oauth, path)
    try:
        usage = anthropic_usage.fetch_usage(path)
        with state.lock:
            state.usage = usage
            state.usage_error = None
            state.usage_updated = time.time()
            state.prev_utilization = {
                w["key"]: w["utilization"] for w in usage["windows"]
            }
        return (usage or {}).get("plan")
    except anthropic_usage.UsageError as exc:
        with state.lock:
            state.usage_error = str(exc)
        return None


CONNECT_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Claude</title>
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
  }
  .wrap { max-width:460px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .muted { color:#8a8478; font-size:.9rem; }
  .card { background:#faf9f5; border:1px solid rgba(61,57,41,.12);
    border-radius:14px; padding:18px; margin-top:16px; }
  .step { font-weight:600; font-size:.8rem; text-transform:uppercase;
    letter-spacing:.05em; color:#c15f3c; margin-bottom:8px; }
  .btn { display:block; width:100%; text-align:center; text-decoration:none;
    background:#d97757; color:#fff; font-weight:600; font-size:1.05rem;
    padding:14px; border-radius:10px; border:none; cursor:pointer; }
  .btn:active { filter:brightness(.94); }
  input { width:100%; padding:12px; font-size:1rem; border-radius:10px;
    border:1px solid rgba(61,57,41,.25); background:#fff; margin-bottom:12px; }
  #msg { margin-top:14px; font-size:.95rem; }
  .ok { color:#0f7b3f; font-weight:600; }
  .err { color:#c4453d; font-weight:600; }
  a.back { color:#c15f3c; }
</style></head>
<body><div class="wrap">
  <h1>Connect your Claude account</h1>
  <p class="muted">Sign in once so the tracker can read your usage limits.
     You can do this right here on your phone.</p>

  <div class="card">
    <div class="step">Step 1</div>
    <a class="btn" id="signin" href="__AUTH_URL__" target="_blank" rel="noopener">
      Sign in with Claude</a>
    <p class="muted" style="margin:12px 0 0">Opens claude.ai. After you approve,
       Claude shows you a code — copy the whole thing.</p>
  </div>

  <div class="card">
    <div class="step">Step 2</div>
    <form id="f" onsubmit="return finish(event)">
      <input id="code" placeholder="Paste the code here"
             autocomplete="off" autocapitalize="off" spellcheck="false">
      <button class="btn" type="submit" id="go">Finish setup</button>
    </form>
    <input type="hidden" id="verifier" value="__VERIFIER__">
    <p class="muted" style="margin:12px 0 0">Codes expire quickly — if it
       fails, just tap Sign in again for a fresh one.</p>
    <div id="msg"></div>
  </div>

  <details class="card" style="margin-top:14px">
    <summary style="cursor:pointer;font-weight:600">Already use Claude Code? Paste its login instead</summary>
    <p class="muted" style="margin:8px 0">This skips the sign-in above (handy if
      it keeps failing). On a computer with Claude Code, open its credentials
      file and paste the whole thing here:</p>
    <p class="muted" style="margin:8px 0;font-size:.82rem">
      macOS: Keychain item <b>Claude Code-credentials</b> ·
      Linux/WSL: <b>~/.claude/.credentials.json</b> ·
      Windows: <b>%USERPROFILE%\\.claude\\.credentials.json</b></p>
    <textarea id="creds" rows="4" placeholder='{"claudeAiOauth": { ... }}'
      style="width:100%;padding:12px;border-radius:10px;border:1px solid rgba(61,57,41,.25);font-family:monospace;font-size:.85rem"></textarea>
    <button class="btn" type="button" onclick="pasteCreds()" style="margin-top:10px">Use these credentials</button>
    <div id="pmsg" style="margin-top:10px;font-weight:600"></div>
  </details>

  <p class="muted" style="margin-top:18px">
    <a class="back" href="#" onclick="return startOver()">Start over with a fresh code</a>
    &nbsp;·&nbsp; <a class="back" href="/">Back to dashboard</a></p>
</div>
<script>
async function pasteCreds(){
  var t=document.getElementById('creds').value.trim();
  var m=document.getElementById('pmsg');
  if(!t){ m.innerHTML='<span class="err">Paste the credentials first.</span>'; return; }
  m.textContent='Saving…';
  try{
    var r=await fetch('/api/paste-creds',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({creds:t})});
    var d=await r.json();
    if(d.ok){ m.innerHTML='<span class="ok">Connected!'+(d.plan?(' ('+d.plan+' plan)'):'')+'</span> Redirecting…';
      setTimeout(function(){location.href='/';},1500); }
    else{ m.innerHTML='<span class="err">'+d.error+'</span>'; }
  }catch(e){ m.innerHTML='<span class="err">Could not reach the tracker.</span>'; }
}
// Keep the sign-in link + PKCE verifier stable across mobile tab reloads:
// if we've already started a flow, reuse it instead of the fresh one the
// server just injected (so the code you got still matches).
(function(){
  var link=document.getElementById('signin'), ver=document.getElementById('verifier');
  var saved=null; try{ saved=JSON.parse(localStorage.getItem('ctp_oauth')||'null'); }catch(e){}
  if(saved&&saved.url&&saved.ver){ link.href=saved.url; ver.value=saved.ver; }
  else{ localStorage.setItem('ctp_oauth', JSON.stringify({url:link.href, ver:ver.value})); }
})();
function startOver(){ try{localStorage.removeItem('ctp_oauth');}catch(e){} location.reload(); return false; }
async function finish(e){
  e.preventDefault();
  var msg=document.getElementById('msg'), go=document.getElementById('go');
  var code=document.getElementById('code').value.trim();
  var verifier=document.getElementById('verifier').value;
  if(!code){ msg.innerHTML='<span class="err">Paste the code first.</span>'; return false; }
  go.disabled=true; msg.textContent='Connecting…';
  try{
    var r=await fetch('/api/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:code,verifier:verifier})});
    var d=await r.json();
    if(d.ok){
      try{localStorage.removeItem('ctp_oauth');}catch(e){}
      msg.innerHTML='<span class="ok">You\\'re connected!'+
        (d.plan?(' ('+d.plan+' plan)'):'')+'</span> Redirecting…';
      setTimeout(function(){location.href='/';},1500);
    } else {
      go.disabled=false;
      msg.innerHTML='<span class="err">'+(d.error||'Sign-in failed.')+'</span>';
    }
  }catch(err){
    go.disabled=false;
    msg.innerHTML='<span class="err">Could not reach the tracker. Try again.</span>';
  }
  return false;
}
</script>
</body></html>"""


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


def make_handler(state, demo):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudeTrackerPi/1.0"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                snapshot = demo_snapshot() if demo else state.snapshot()
                self._send_json(snapshot)
                return
            if path == "/connect":
                self._send_connect_page()
                return
            if path == "/wifi":
                self._send_html(WIFI_PAGE)
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
            if path == "/api/connect":
                self._handle_connect()
                return
            if path == "/api/paste-creds":
                self._handle_paste()
                return
            if path == "/api/push":
                self._handle_push()
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

        def _send_connect_page(self):
            url, verifier = oauth_login.start_login()
            with state.lock:
                state.pending_verifier = verifier
            body = CONNECT_PAGE.replace(
                "__AUTH_URL__", html.escape(url, quote=True)
            ).replace("__VERIFIER__", html.escape(verifier, quote=True))
            body = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_connect(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                data = {}
            if demo:
                self._send_json({"ok": False,
                                 "error": "Sign-in is disabled in demo mode."})
                return
            try:
                plan = complete_login(state, data.get("code", ""),
                                      data.get("verifier"))
                self._send_json({"ok": True, "plan": plan})
            except oauth_login.LoginError as exc:
                self._send_json({"ok": False, "error": str(exc)})
            except Exception as exc:  # never crash the handler
                self._send_json({"ok": False,
                                 "error": f"Unexpected error: {exc}"})

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

        def _handle_paste(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                data = {}
            if demo:
                self._send_json({"ok": False,
                                 "error": "Disabled in demo mode."})
                return
            try:
                plan = complete_paste(state, data.get("creds", ""))
                self._send_json({"ok": True, "plan": plan})
            except oauth_login.LoginError as exc:
                self._send_json({"ok": False, "error": str(exc)})
            except Exception as exc:
                self._send_json({"ok": False,
                                 "error": f"Unexpected error: {exc}"})

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
