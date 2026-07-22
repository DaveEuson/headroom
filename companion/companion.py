#!/usr/bin/env python3
"""Headroom companion — runs on the computer where you use Claude Code.

Reads the *real* Claude subscription usage numbers and pushes them to the Pi.

How (and why it works when a fresh sign-in doesn't): it never signs in. It
reuses Claude Code's own existing login — the credentials Claude Code already
saved on this machine — refreshes that token if needed, and reads Anthropic's
usage endpoint. It never touches the authorization-code sign-in exchange, which
is the throttled one. (Same approach as the Sparko "Fuel" widget.)

    Credentials:  macOS Keychain item "Claude Code-credentials", else
                  ~/.claude/.credentials.json  (Windows/Linux)
    Refresh:      POST https://platform.claude.com/v1/oauth/token (refresh_token)
    Usage:        GET  https://api.anthropic.com/api/oauth/usage

If it can't read credentials (Claude Code not logged in here), it falls back to
estimating usage from Claude Code's local logs.

Run it:  python3 companion.py --pi http://claudecounter.local:8080
Standard library only, Python 3.8+.
"""

import argparse
import concurrent.futures
import datetime
import glob
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_MARKER = "Headroom"  # /api/status "app" field, used for discovery

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "Headroom-Companion/1.0"
KEYCHAIN_SERVICE = "Claude Code-credentials"
REFRESH_MARGIN = 300  # refresh if the token expires within 5 minutes

WINDOW_LABELS = {
    "five_hour": "Session (5 hour)",
    "seven_day": "Weekly (all models)",
    "seven_day_sonnet": "Weekly (Sonnet)",
    "seven_day_opus": "Weekly (Opus)",
    "seven_day_oauth_apps": "Weekly (connected apps)",
    "extra_usage": "Extra usage",
}

# Fallback-only: rough token budgets if we must estimate from logs. Anthropic
# doesn't publish real caps, so these are ballpark; "max" is ~5x "pro". Only
# used when there's no Claude Code login to read the real numbers from.
PLAN_PRESETS = {
    "max": {
        "five_hour": 220_000_000,
        "seven_day": 1_500_000_000,
        "seven_day_opus": 300_000_000,
    },
    "pro": {
        "five_hour": 44_000_000,
        "seven_day": 300_000_000,
        "seven_day_opus": 60_000_000,
    },
}
DEFAULT_LIMITS = PLAN_PRESETS["max"]
FIVE_HOURS, SEVEN_DAYS = 5 * 3600, 7 * 86400


# ----------------------------------------------------- credentials (like Sparko)

def _creds_file():
    return os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")


def read_creds():
    """Return (creds, save_fn) or (None, None). creds has accessToken/
    refreshToken/expiresAt(ms). save_fn persists an updated oauth dict."""
    # macOS Keychain
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                creds = _parse_creds(out.stdout)
                if creds:
                    def save(oauth):
                        blob = json.dumps({"claudeAiOauth": oauth})
                        subprocess.run(
                            ["security", "add-generic-password", "-U",
                             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_SERVICE,
                             "-w", blob], capture_output=True, timeout=5)
                    return creds, save
        except (OSError, subprocess.SubprocessError):
            pass
    # file (Windows / Linux)
    path = _creds_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            creds = _parse_creds(fh.read())
        if creds:
            def save(oauth):
                data = {"claudeAiOauth": oauth}
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            return creds, save
    except OSError:
        pass
    return None, None


def _parse_creds(raw):
    try:
        j = json.loads(raw)
    except ValueError:
        return None
    o = j.get("claudeAiOauth") if isinstance(j, dict) else None
    o = o or j
    access = o.get("accessToken") or o.get("access_token")
    if not access:
        return None
    expires = o.get("expiresAt") or o.get("expires_at") or 0
    return {
        "accessToken": access,
        "refreshToken": o.get("refreshToken") or o.get("refresh_token"),
        "expiresAt": int(expires),  # epoch ms
        "subscriptionType": o.get("subscriptionType"),
        "_raw": o,
    }


def valid_token(creds, save_fn):
    """Return a usable access token, refreshing only if expired. None on fail."""
    exp_s = creds["expiresAt"] / 1000.0 if creds["expiresAt"] else 0
    if exp_s and exp_s - REFRESH_MARGIN > time.time():
        return creds["accessToken"]            # still fresh — pure read
    if not creds["refreshToken"]:
        return creds["accessToken"] if not exp_s else None
    # Rotating refresh tokens: only refresh if we can write the new one back,
    # so we never leave Claude Code with a dead token.
    try:
        body = json.dumps({"grant_type": "refresh_token",
                           "refresh_token": creds["refreshToken"],
                           "client_id": CLIENT_ID}).encode("utf-8")
        req = urllib.request.Request(
            REFRESH_URL, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"token refresh failed ({exc}); Claude Code can refresh it by "
              "running any command.", file=sys.stderr)
        return None
    oauth = dict(creds["_raw"])
    oauth["accessToken"] = result["access_token"]
    if result.get("refresh_token"):
        oauth["refreshToken"] = result["refresh_token"]
    if result.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + result["expires_in"]) * 1000)
    try:
        save_fn(oauth)
    except Exception as exc:  # noqa: BLE001 - don't lose the token on write fail
        print(f"warning: refreshed but couldn't save back ({exc})",
              file=sys.stderr)
    return oauth["accessToken"]


def fetch_usage(token):
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}",
                 "anthropic-beta": OAUTH_BETA,
                 "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def windows_from_usage(raw):
    order = list(WINDOW_LABELS)
    out = []
    for key, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        util = value.get("utilization")
        if util is None:
            continue
        try:
            util = max(0.0, min(100.0, float(util)))
        except (TypeError, ValueError):
            continue
        out.append({
            "key": key,
            "label": WINDOW_LABELS.get(key, key.replace("_", " ").title()),
            "utilization": round(util, 1),
            "resets_at": value.get("resets_at") or value.get("resetsAt"),
        })
    out.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    return out


class LiveUnavailable(Exception):
    """A Claude Code login exists but live usage is temporarily unreadable.
    We must NOT fall back to log estimates in this case — stale real numbers
    on the tracker beat fresh wrong ones."""

    def __init__(self, msg, retry_after=0):
        super().__init__(msg)
        self.retry_after = retry_after


def get_live_windows():
    """Real usage via Claude Code's login. Returns (windows, plan), or None
    when there's no login at all. Raises LiveUnavailable on transient failure."""
    creds, save_fn = read_creds()
    if not creds:
        return None
    token = valid_token(creds, save_fn)
    if not token:
        raise LiveUnavailable(
            "couldn't refresh the Claude Code token; run `claude` on this "
            "computer once to refresh the login")
    try:
        raw = fetch_usage(token)
    except urllib.error.HTTPError as exc:
        retry_after = 0
        try:
            retry_after = int(exc.headers.get("Retry-After", 0) or 0)
        except (TypeError, ValueError):
            pass
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise LiveUnavailable(
            f"usage endpoint returned HTTP {exc.code}"
            + (f", retry after {retry_after}s" if retry_after else "")
            + (f" — {detail}" if detail else ""),
            retry_after=retry_after)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LiveUnavailable(f"couldn't read usage: {exc}")
    windows = windows_from_usage(raw)
    if not windows:
        raise LiveUnavailable("usage response had no windows")
    return windows, creds.get("subscriptionType")


# ------------------------------------------------- fallback: estimate from logs

def _parse_ts(value):
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def read_events(root):
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    ts = _parse_ts(entry.get("timestamp"))
                    if not usage or ts is None:
                        continue
                    tokens = sum(usage.get(k, 0) or 0 for k in (
                        "input_tokens", "output_tokens",
                        "cache_creation_input_tokens", "cache_read_input_tokens"))
                    model = (msg.get("model") or "").lower()
                    yield ts, model, tokens
        except OSError:
            continue


def get_log_windows(limits):
    root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(root):
        return None
    events = list(read_events(root))
    now = time.time()

    def win(seconds, key, label, subset=None):
        cutoff, total, oldest = now - seconds, 0, None
        for ts, model, tok in events:
            if ts >= cutoff and (subset is None or subset in model):
                total += tok
                oldest = ts if oldest is None else min(oldest, ts)
        iso = (datetime.datetime.fromtimestamp(oldest + seconds,
               datetime.timezone.utc).isoformat() if oldest else None)
        return {"key": key, "label": label,
                "utilization": round(min(100.0, 100.0 * total / max(1, limits[key])), 1),
                "resets_at": iso}

    windows = [win(FIVE_HOURS, "five_hour", "Session (5 hour)"),
               win(SEVEN_DAYS, "seven_day", "Weekly (all models)")]
    opus = win(SEVEN_DAYS, "seven_day_opus", "Weekly (Opus)", subset="opus")
    if opus["utilization"] > 0:
        windows.append(opus)
    return windows


# ------------------------------------------------------------------- push loop

def push(pi_url, token, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Push-Token"] = token
    req = urllib.request.Request(pi_url.rstrip("/") + "/api/push",
                                 data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pair_device(url):
    """Hand this computer's existing Claude login to a board (Headroom Mini) so
    it can poll usage on its own — the user never copies a token by hand."""
    creds, _ = read_creds()
    if not creds:
        _print_no_claude()
        return False
    oauth = {
        "accessToken": creds["accessToken"],
        "refreshToken": creds.get("refreshToken"),
        "expiresAt": creds.get("expiresAt", 0),
        "subscriptionType": creds.get("subscriptionType"),
    }
    body = json.dumps(oauth).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/api/pair", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Couldn't reach the board at {url}: {exc}", file=sys.stderr)
        return False
    if not result.get("ok"):
        print(f"Board rejected pairing: {result.get('error')}", file=sys.stderr)
        return False
    live = result.get("live")
    print(f"Paired {url} — the board updates itself now"
          + ("." if live else " (first read pending; it will retry)."))
    print("Tip: use a SEPARATE Claude login for the board. If it shares this "
          "computer's login, the two will rotate each other's token and log "
          "each other out.")
    return True


def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "companion.config.json")


# ------------------------------------------------------ auto-discover the Pi

def _probe(url):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/status")
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("app") in (APP_MARKER, "ClaudeTrackerPi")  # accept old
    except Exception:
        return False


def discover_pi(port=8080):
    """Find the tracker on the LAN with no address typing. Returns URL or None."""
    for host in ("claudetracker.local", "claudecounter.local"):
        url = f"http://{host}:{port}"
        if _probe(url):
            return url
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        prefix = sock.getsockname()[0].rsplit(".", 1)[0]
        sock.close()
    except OSError:
        return None
    urls = [f"http://{prefix}.{i}:{port}" for i in range(1, 255)]
    print("Looking for your tracker on the network...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        futures = {ex.submit(_probe, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            try:
                if fut.result():
                    return futures[fut]
            except Exception:
                pass
    return None


def save_pi(url):
    path = _config_path()
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    data["pi"] = url
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------- run on every login

INSTALLED_MARKER = os.path.expanduser("~/.claudetracker-companion-installed")


def install_autostart():
    """Set the companion to launch at login. Returns a human-readable path."""
    script = os.path.abspath(__file__)
    py = sys.executable or "python3"
    if sys.platform == "win32":
        pyw = py[:-len("python.exe")] + "pythonw.exe" \
            if py.lower().endswith("python.exe") else py
        startup = os.path.join(os.environ.get("APPDATA", ""), "Microsoft",
                               "Windows", "Start Menu", "Programs", "Startup")
        os.makedirs(startup, exist_ok=True)
        target = os.path.join(startup, "HeadroomCompanion.bat")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(f'@echo off\r\nstart "" "{pyw}" "{script}"\r\n')
        return target
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, "com.claudetracker.companion.plist")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claudetracker.companion</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>{script}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>""")
        subprocess.run(["launchctl", "unload", target],
                       capture_output=True)
        subprocess.run(["launchctl", "load", target], capture_output=True)
        return target
    # linux
    d = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, "claudetracker-companion.service")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(f"""[Unit]
Description=Headroom companion
After=network-online.target

[Service]
ExecStart={py} {script}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "enable", "--now",
                    "claudetracker-companion"], capture_output=True)
    return target


def uninstall_autostart():
    removed = []
    for p in (
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                     "Start Menu", "Programs", "Startup",
                     "HeadroomCompanion.bat"),
        os.path.expanduser("~/Library/LaunchAgents/"
                           "com.claudetracker.companion.plist"),
        os.path.expanduser("~/.config/systemd/user/"
                           "claudetracker-companion.service"),
    ):
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed.append(p)
            except OSError:
                pass
    for m in (INSTALLED_MARKER,):
        if os.path.isfile(m):
            os.remove(m)
    return removed


def load_config():
    cfg = {"pi": None, "token": "", "interval": 120, "plan": "max"}
    data = {}
    path = _config_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            cfg.update({k: data[k] for k in ("pi", "token", "interval")
                        if k in data})
            if data.get("plan") in PLAN_PRESETS:
                cfg["plan"] = data["plan"]
        except (OSError, ValueError) as exc:
            print(f"Ignoring bad companion.config.json: {exc}", file=sys.stderr)
    # Estimation budgets: start from the plan preset, then apply any overrides.
    cfg["limits"] = dict(PLAN_PRESETS[cfg["plan"]])
    if isinstance(data.get("limits"), dict):
        cfg["limits"].update(data["limits"])
    return cfg


def _print_no_claude():
    """A clear, actionable message when there's no Claude Code login to read."""
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude")
    print("", file=sys.stderr)
    print("Can't find your Claude usage on this computer.", file=sys.stderr)
    if os.path.isdir(claude_dir):
        print("  Claude Code is installed here, but you're not signed in.",
              file=sys.stderr)
        print("  Fix: open a terminal, run  claude  , then type  /login .",
              file=sys.stderr)
    else:
        print("  Claude Code (the CLI) isn't installed on this computer.",
              file=sys.stderr)
        print("  The tracker reads Claude Code's own login to get your real",
              file=sys.stderr)
        print("  usage, so it needs Claude Code installed and signed in HERE.",
              file=sys.stderr)
        print("  Install:  npm install -g @anthropic-ai/claude-code",
              file=sys.stderr)
        print("  then run  claude  and type  /login .", file=sys.stderr)
    print("  (Run this companion on the same computer where you use Claude "
          "Code — not on the Pi.)", file=sys.stderr)


def run_once(cfg):
    """One poll+push cycle. Returns (ok, extra_sleep_seconds)."""
    try:
        live = get_live_windows()
    except LiveUnavailable as exc:
        # A login exists but live usage is temporarily unreadable (rate
        # limit, network blip). Skip this push: the tracker keeps showing
        # the last REAL numbers instead of wrong log-based estimates.
        print(f"live usage unavailable ({exc}); skipping this push so the "
              "tracker keeps its last real reading", file=sys.stderr)
        return False, min(900, exc.retry_after)
    if live:
        windows, plan = live
        source = "live"
    else:
        # No Claude Code login on this machine at all -> estimation is the
        # best we can do (clearly tagged as such).
        windows = get_log_windows(cfg["limits"])
        plan, source = None, "estimated"
        if not windows:
            _print_no_claude()
            return False, 0
    payload = {"windows": windows, "plan": plan, "source": source}
    # cfg["pi"] may be a comma-separated list — one companion can feed
    # several trackers (e.g. a Pi on the desk and a Mini on the shelf).
    targets = [t.strip() for t in str(cfg["pi"]).split(",") if t.strip()]
    delivered = 0
    for target in targets:
        try:
            result = push(target, cfg["token"], payload)
        except (urllib.error.URLError, OSError) as exc:
            print(f"Couldn't reach the tracker at {target}: {exc}",
                  file=sys.stderr)
            continue
        if result.get("ok"):
            delivered += 1
        else:
            print(f"{target} rejected the push: {result.get('error')}",
                  file=sys.stderr)
    if delivered == 0:
        return False, 0
    tag = "LIVE" if source == "live" else "estimated"
    summary = ", ".join(f"{w['label'].split(' (')[0]} {w['utilization']}%"
                        for w in windows)
    where = f" -> {delivered}/{len(targets)} trackers" if len(targets) > 1 else ""
    print(f"pushed [{tag}]{where}: {summary}")
    return True, 0


LOCK_PORT = 47823   # localhost mutex so two companions can't double-poll


def _single_instance():
    """Bind a localhost port as a process-wide lock. Returns the socket to
    hold for our lifetime, or None if another companion already has it."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Headroom companion")
    ap.add_argument("--pi", default=cfg["pi"],
                    help="tracker URL(s), comma-separated for multiple "
                         "devices (auto-discovered if omitted)")
    ap.add_argument("--token", default=cfg["token"])
    ap.add_argument("--interval", type=int, default=cfg["interval"])
    ap.add_argument("--once", action="store_true", help="push once and exit")
    ap.add_argument("--pair", nargs="?", const="", default=None, metavar="URL",
                    help="send this computer's Claude login to a board so it "
                         "runs self-contained, then exit (board auto-found if "
                         "no URL is given)")
    ap.add_argument("--no-install", action="store_true",
                    help="don't add to startup")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove from startup and exit")
    args = ap.parse_args()

    if args.pair is not None:
        url = args.pair
        if not url:
            print("Looking for your board on the network...")
            url = discover_pi()
        if not url:
            ap.error("couldn't find a board on your network. Make sure it's "
                     "powered on and on the same Wi-Fi, or pass the address "
                     "shown on its screen: --pair http://<its-address>:8080")
        sys.exit(0 if pair_device(url) else 1)

    if args.uninstall:
        removed = uninstall_autostart()
        print("Removed:\n  " + "\n  ".join(removed) if removed
              else "Nothing to remove.")
        return

    cfg["pi"], cfg["token"], cfg["interval"] = args.pi, args.token, args.interval
    if not cfg["pi"]:
        cfg["pi"] = discover_pi()
        if cfg["pi"]:
            print(f"Found your tracker at {cfg['pi']}")
            save_pi(cfg["pi"])
        else:
            ap.error("couldn't find the tracker on your network. Make sure it's "
                     "powered on and on the same Wi-Fi, or pass "
                     "--pi http://<its-address>:8080")

    if args.once:
        print(f"Headroom companion -> {cfg['pi']} (single push)")
        ok, _ = run_once(cfg)
        sys.exit(0 if ok else 1)

    lock = _single_instance()
    if lock is None:
        print("Another Headroom companion is already running on this computer "
              "(probably the auto-started one) — exiting so we don't "
              "double-poll Anthropic. To run this one instead, stop the other "
              "first (or reboot after --uninstall).")
        return

    print(f"Headroom companion -> {cfg['pi']} (every {cfg['interval']}s)")
    first_ok, _ = run_once(cfg)
    if first_ok and not args.no_install and not os.path.isfile(INSTALLED_MARKER):
        try:
            where = install_autostart()
            with open(INSTALLED_MARKER, "w", encoding="utf-8") as fh:
                fh.write(cfg["pi"])
            print(f"Set to run automatically at login.\n  {where}\n"
                  "  (run with --uninstall to stop)")
        except Exception as exc:  # noqa: BLE001
            print(f"(couldn't set auto-start: {exc})", file=sys.stderr)
    while True:
        time.sleep(max(30, cfg["interval"]))
        _ok, extra = run_once(cfg)
        if extra:
            print(f"backing off {extra}s (rate limited)", file=sys.stderr)
            time.sleep(extra)


if __name__ == "__main__":
    main()
