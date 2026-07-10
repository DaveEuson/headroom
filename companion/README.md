# ClaudeTracker companion

Runs on the **computer where you use Claude Code** and feeds your usage to the
Pi.

## Why this exists

The Pi can't sign in to Anthropic directly — the fresh OAuth sign-in endpoint
is heavily throttled for anything that isn't Claude Code itself. So instead of
signing in, this script **reuses Claude Code's login that's already on your
computer**: it reads the token Claude Code saved, refreshes it if needed, and
reads Anthropic's real usage endpoint — the exact numbers Claude Code's own
`/usage` shows. It then pushes those to the Pi every couple of minutes. It
never does a sign-in, so it never hits the throttle. (Same technique the
Sparko "Fuel" widget uses.)

If Claude Code isn't logged in on this machine, it falls back to *estimating*
usage from Claude Code's local logs (`~/.claude/projects`).

## Setup

You need Python 3 (already on macOS/Linux; on Windows install from
[python.org](https://www.python.org/downloads/) and tick "Add to PATH").
*(Want a no-Python double-click app for end users? See [BUILD.md](BUILD.md).)*

1. Copy this `companion/` folder to the computer where you run Claude Code
   (or clone the repo there).
2. Run it:

   ```bash
   python3 companion.py
   ```

That's it. With no arguments it **finds the tracker on your network by itself**,
starts feeding it, and **sets itself to run at every login** — so you do this
once and never again. (If auto-discovery can't find it, pass
`--pi http://<its-address>:8080`, shown on the tracker screen.)

- Stop it auto-running: `python3 companion.py --uninstall`
- Don't auto-install in the first place: add `--no-install`

## Configure without flags

Copy `companion.config.example.json` to `companion.config.json` next to
`companion.py` and fill it in:

```json
{
  "pi": "http://claudecounter.local:8080",
  "token": "",
  "interval": 120,
  "plan": "max",
  "limits": { "five_hour": 220000000, "seven_day": 1500000000, "seven_day_opus": 300000000 }
}
```

## About the percentages

When Claude Code is logged in on this machine (the normal case), the numbers
are **the real thing** — Anthropic's own utilization windows, identical to what
`claude /usage` shows. The companion prints `pushed [LIVE]: …` when it's using
them.

Only if it *can't* read a Claude Code login does it fall back to *estimating*
from local logs (`pushed [estimated]: …`). In that mode the percentages are
measured against the token budgets in `limits`, which you can tune — the reset
timing and usage amounts are still accurate, just the % is a guess.

**Requires Claude Code signed in on this computer** for live numbers. If Sparko's
"Fuel" widget works for you, you already have this.

## Security

If your Pi's `config.json` sets a `push_token`, pass the same value with
`--token` (or in the config file). Then only your companion can post data to
the Pi. On a home network it's optional but nice to have.
