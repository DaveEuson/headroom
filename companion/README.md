# Headroom companion

Runs on the **computer where you use Claude Code** and feeds your usage to the
Pi.

## Tray app (menubar / system tray)

`tray.py` is the friendly version — it lives in your menubar (macOS) or system
tray (Windows/Linux) instead of a terminal. The icon is green when it's feeding
the board, amber while it's searching, red when it's stuck; the menu has
one-click **Pair** (make the board self-contained), **Open board page**, and a
**Feeding** toggle. It reuses everything below.

```
pip install pystray pillow certifi        # + pyobjc-framework-Cocoa on macOS
python tray.py
```

(A packaged, double-click build will ship with a release once it's verified on
each OS — until then, run it from source as above.)

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

### Easiest: the double-click app (no Python)

Scan the QR code on the tracker's screen — or open
`http://<tracker-address>:8080/setup` — and download the companion for your OS
from the [latest release]. Double-click it. Done.

[latest release]: https://github.com/DaveEuson/HeadroomMini/releases/latest

On first run it **finds the tracker on your network by itself**, sends the first
reading, and **sets itself to run at every login** — so you do this once and
never again. (First launch: macOS → right-click → **Open**; Windows → **More
info → Run anyway**, since the binary isn't code-signed yet.)

These apps are produced automatically by CI on every tagged release
(see [BUILD.md](BUILD.md) and `.github/workflows/release.yml`).

### Or run the script

You need Python 3 (already on macOS/Linux; on Windows install from
[python.org](https://www.python.org/downloads/) and tick "Add to PATH").

1. Copy this `companion/` folder to the computer where you run Claude Code
   (or clone the repo there).
2. Run it:

   ```bash
   python3 companion.py
   ```

Same behavior: auto-discovers the tracker, feeds it, and installs itself to run
at login. (If auto-discovery can't find it, pass
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
  "plan": "max"
}
```

`"plan"` is `"max"` or `"pro"` and just picks the fallback estimation budgets
(Pro's are ~1/5 of Max's). It only matters when there's no Claude Code login to
read — live numbers ignore it. Add an explicit `"limits": { … }` object to
override the budgets by hand.

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
