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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic_usage
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

    def snapshot(self):
        with self.lock:
            return {
                "windows": (self.usage or {}).get("windows", []),
                "plan": (self.usage or {}).get("plan"),
                "usage_error": self.usage_error,
                "usage_updated": self.usage_updated,
                "battery": self.battery if self.battery_present else None,
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
        "session_active": True,
        "night": {"start": "22:00", "end": "07:00"},
        "server_time": t,
    }


def start_pollers(state, config):
    creds_configured = config.get("credentials_path")

    def poll_usage():
        while True:
            path = anthropic_usage.find_credentials_path(creds_configured)
            if path is None:
                message = (
                    "No Claude credentials found on this Pi. Run "
                    "scripts/send-credentials.sh from the computer where you "
                    "use Claude Code (see README)."
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

    for target in (poll_usage, poll_battery):
        threading.Thread(target=target, daemon=True).start()


def make_handler(state, demo):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClaudeTrackerPi/1.0"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                snapshot = demo_snapshot() if demo else state.snapshot()
                self._send_json(snapshot)
                return
            if path == "/":
                path = "/index.html"
            self._send_file(path.lstrip("/"))

        def _send_json(self, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
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
