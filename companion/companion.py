#!/usr/bin/env python3
"""ClaudeTracker companion — runs on the computer where you use Claude Code.

Anthropic blocks third-party tools from its OAuth/usage endpoints, so the Pi
can't ask Anthropic directly. Instead, this little script reads Claude Code's
own local session logs on your machine (~/.claude/projects/**/*.jsonl — files
Claude Code writes itself, no network, no login), works out how much you've
used in the current 5-hour block and the last 7 days, and POSTs that to your
Pi every couple of minutes. The Pi just displays it.

Standard library only. Run it with Python 3.8+:

    python3 companion.py --pi http://claudecounter.local:8080

Options (or set in companion.config.json next to this file):
    --pi URL        your Pi's dashboard address (required)
    --token SECRET  must match "push_token" in the Pi's config.json (optional)
    --once          compute and push a single time, then exit (for testing)

Because Anthropic doesn't publish exact token limits, the percentages are
measured against the budgets in DEFAULT_LIMITS below — tune them to your plan.
The reset countdowns and "how much you've used" are read straight from the
logs and are accurate.
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Rough token budgets per window. Anthropic doesn't publish real numbers, so
# treat these as dials — raise/lower until the meters feel right for your plan.
DEFAULT_LIMITS = {
    "five_hour": 220_000_000,     # 5-hour session block
    "seven_day": 1_500_000_000,   # weekly, all models
    "seven_day_opus": 300_000_000,  # weekly, Opus only
}

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400


def claude_projects_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, TypeError):
        return None


def read_events(root):
    """Yield (timestamp, model, tokens) from every assistant message on disk."""
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    msg = entry.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    ts = _parse_ts(entry.get("timestamp"))
                    if ts is None:
                        continue
                    tokens = (
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                    )
                    model = (msg.get("model") or entry.get("model") or "").lower()
                    yield ts, model, tokens
        except OSError:
            continue


def _window(events, seconds, now):
    """Sum tokens within the last `seconds`; return (tokens, oldest_ts)."""
    cutoff = now - seconds
    total = 0
    oldest = None
    for ts, _model, tokens in events:
        if ts >= cutoff:
            total += tokens
            oldest = ts if oldest is None else min(oldest, ts)
    return total, oldest


def compute_windows(events, limits, now=None):
    now = now or time.time()
    events = list(events)

    def window(seconds, subset=None):
        subset_events = (
            events if subset is None
            else [e for e in events if subset in e[1]]
        )
        return _window(subset_events, seconds, now)

    def iso(ts):
        return datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).isoformat()

    windows = []

    tok, oldest = window(FIVE_HOURS)
    windows.append({
        "key": "five_hour", "label": "Session (5 hour)",
        "utilization": 100.0 * tok / max(1, limits["five_hour"]),
        "resets_at": iso(oldest + FIVE_HOURS) if oldest else None,
    })

    tok, oldest = window(SEVEN_DAYS)
    windows.append({
        "key": "seven_day", "label": "Weekly (all models)",
        "utilization": 100.0 * tok / max(1, limits["seven_day"]),
        "resets_at": iso(oldest + SEVEN_DAYS) if oldest else None,
    })

    tok, oldest = window(SEVEN_DAYS, subset="opus")
    if tok:
        windows.append({
            "key": "seven_day_opus", "label": "Weekly (Opus)",
            "utilization": 100.0 * tok / max(1, limits["seven_day_opus"]),
            "resets_at": iso(oldest + SEVEN_DAYS) if oldest else None,
        })

    for w in windows:
        w["utilization"] = round(min(100.0, w["utilization"]), 1)
    return windows


def push(pi_url, token, payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Push-Token"] = token
    req = urllib.request.Request(
        pi_url.rstrip("/") + "/api/push", data=body, headers=headers,
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_config():
    cfg = {"pi": None, "token": "", "interval": 120, "plan": None,
           "limits": dict(DEFAULT_LIMITS)}
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "companion.config.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            cfg.update({k: data[k] for k in cfg if k in data})
            if isinstance(data.get("limits"), dict):
                cfg["limits"].update(data["limits"])
        except (OSError, ValueError) as exc:
            print(f"Ignoring bad companion.config.json: {exc}", file=sys.stderr)
    return cfg


def run_once(cfg, last_total=[0]):
    root = claude_projects_dir()
    if not os.path.isdir(root):
        print(f"Claude Code logs not found at {root}. Is Claude Code installed "
              "and used on this machine?", file=sys.stderr)
        return False
    events = list(read_events(root))
    windows = compute_windows(events, cfg["limits"])
    total = sum(t for _ts, _m, t in events)
    payload = {"windows": windows, "plan": cfg.get("plan"),
               "active": total > last_total[0]}
    last_total[0] = total
    try:
        result = push(cfg["pi"], cfg["token"], payload)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Couldn't reach the Pi at {cfg['pi']}: {exc}", file=sys.stderr)
        return False
    if not result.get("ok"):
        print(f"Pi rejected the push: {result.get('error')}", file=sys.stderr)
        return False
    top = ", ".join(f"{w['label'].split(' (')[0]} {w['utilization']}%"
                    for w in windows)
    print(f"pushed: {top}")
    return True


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="ClaudeTracker companion")
    ap.add_argument("--pi", default=cfg["pi"],
                    help="Pi dashboard URL, e.g. http://claudecounter.local:8080")
    ap.add_argument("--token", default=cfg["token"])
    ap.add_argument("--interval", type=int, default=cfg["interval"])
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    cfg["pi"], cfg["token"], cfg["interval"] = args.pi, args.token, args.interval

    if not cfg["pi"]:
        ap.error("no Pi address. Pass --pi http://<pi>:8080 or set it in "
                 "companion.config.json")

    print(f"ClaudeTracker companion -> {cfg['pi']} "
          f"(every {cfg['interval']}s). Reading {claude_projects_dir()}")
    if args.once:
        sys.exit(0 if run_once(cfg) else 1)
    while True:
        run_once(cfg)
        time.sleep(max(30, cfg["interval"]))


if __name__ == "__main__":
    main()
