# ClaudeTracker companion

Runs on the **computer where you use Claude Code** and feeds your usage to the
Pi.

## Why this exists

Anthropic blocks third-party tools from its sign-in and usage endpoints (the
policy change that landed in early 2026), so the Pi can't ask Anthropic
directly. Instead, this small script reads Claude Code's **own local session
logs** on your machine — the `~/.claude/projects/**/*.jsonl` files Claude Code
writes itself — figures out how much you've used in the current 5-hour block
and the last 7 days, and sends that to your Pi every couple of minutes. No
login, no network calls to Anthropic, nothing against the rules.

## Setup

You need Python 3 (already on macOS/Linux; on Windows install from
[python.org](https://www.python.org/downloads/) and tick "Add to PATH").

1. Copy this `companion/` folder to the computer where you run Claude Code
   (or clone the repo there).
2. Run it, pointing at your Pi:

   ```bash
   python3 companion.py --pi http://claudecounter.local:8080
   ```

   (use your Pi's address — the tracker screen shows it). It prints a line
   every couple of minutes as it pushes; leave it running.

That's it — the Pi's meters go live.

## Keep it running automatically

- **macOS/Linux:** add it to your login items / a cron `@reboot`, or run under
  `tmux`/`screen`.
- **Windows:** put a shortcut to
  `pythonw companion.py --pi http://claudecounter.local:8080` in your Startup
  folder (`Win+R` → `shell:startup`). `pythonw` runs it without a console
  window.

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

The **reset countdowns and how much you've used** are read straight from Claude
Code's logs and are accurate. The **percentages** are measured against the
token budgets in `limits`, because Anthropic doesn't publish the real per-window
caps. Treat those numbers as dials: if a meter reads high or low compared to
what Claude Code's own `/usage` shows, nudge the matching `limits` value until
it lines up. The trend, the reset timing, and the Opus-vs-all split are all
real either way.

## Security

If your Pi's `config.json` sets a `push_token`, pass the same value with
`--token` (or in the config file). Then only your companion can post data to
the Pi. On a home network it's optional but nice to have.
