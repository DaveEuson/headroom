# ClaudeTrackerPi

A tiny always-on dashboard for your **Claude usage limits**, built for a
**Raspberry Pi Zero 2 W** with a **PiSugar** battery. Glance at it and know
how much usage you have left, when it resets, and whether Claude is being
used right now — no terminal, no menubar, no estimating.

<p align="center">
  <img src="docs/screenshots/day.png" width="380" alt="Day mode — cream Claude theme, Pip dancing">
  &nbsp;
  <img src="docs/screenshots/night.png" width="380" alt="Night mode — dark Claude theme, Pip asleep">
</p>

## What you get

- **Meters and percentages** for every usage window Claude reports (5-hour
  session, weekly, weekly Opus, …) — fuel-gauge style, so the bar shows how
  much you have **left**; amber under 30%, red under 10%
- **Reset countdowns** — live "resets in 2h 13m" plus the local clock time
- **A clock**, and Claude's own look: the warm cream claude.ai theme by day,
  its dark theme at night (on a configurable schedule)
- **PiSugar battery level** with a charging indicator
- **Whisplay HAT support** — draws a compact version of the dashboard
  right on the PiSugar Whisplay's 1.69″ LCD (240×280, ST7789), no browser
  or desktop needed on the Pi
- **QR-code setup + phone sign-in** — until it has your Claude login, the HAT
  screen shows a "Set me up" QR code; scan it, tap **Sign in with Claude**, and
  authenticate right on your phone (OAuth, the same login Claude Code uses) —
  no computer, no scripts, no files to copy
- **Pip.** The tracker's mascot: a little retro computer. Between sessions
  it chills with a sunglasses screensaver; the moment it detects you're
  actually using Claude it starts dancing while code flies across its
  screen. The screen phosphor turns amber when you're running low and red
  when you're almost out, and at night it puts on a nightcap and sleeps
  with a crescent moon on screen.

<p align="center">
  <img src="docs/screenshots/moods.png" width="720" alt="Pip's five moods: chilling, dancing, running low, almost out, and asleep">
</p>

No pip installs, no Node, no database — one Python 3 standard-library
process serving one page. It idles in a few MB of RAM on the Zero 2 W, and
you view it from any browser on your network (phone, laptop, or a small
screen on the Pi itself in kiosk mode).

## Setup (two steps)

### 1. On the Pi

```bash
git clone https://github.com/DaveEuson/ClaudeTrackerPi.git
cd ClaudeTrackerPi
./install.sh
```

That creates a `claude-tracker` systemd service that starts on boot. The
installer prints your dashboard URL, e.g. `http://raspberrypi.local:8080`.

### 2. Sign in from your phone

Open the dashboard (the HAT screen shows a QR code, or go to the URL the
installer printed) and tap **Sign in with Claude**. That opens claude.ai;
approve, copy the code it shows you, paste it back into the dashboard, and
you're connected. No computer, no scripts, no files to copy.

That's it — the meters go live immediately. The tracker refreshes its token
automatically from then on, so this is a one-time step. If the sign-in ever
expires, the dashboard shows the button again.

> **Prefer to reuse an existing Claude Code login?** Two shortcuts:
> - Already run Claude Code *on the Pi itself*? The tracker auto-detects
>   `~/.claude/.credentials.json` — nothing to do.
> - On a Mac/Linux box with Claude Code logged in? You can copy its
>   credentials over instead: `./scripts/send-credentials.sh pi@raspberrypi.local`

## Try it without a Pi

```bash
python3 app/main.py --demo
```

Serves fake data on http://localhost:8080 so you can see the dashboard (and
Pip's dance moves) from any machine. Add `?night=1` to preview night mode
and `?active=0` to see Pip chilling.

## PiSugar battery

If the [PiSugar power manager](https://github.com/PiSugar/pisugar-power-manager-rs)
is installed (`wget https://cdn.pisugar.com/release/pisugar-power-manager.sh -O - | sudo bash`),
the dashboard shows a battery tile automatically. No PiSugar? The tile just
stays hidden — nothing to configure.

## Configuration (optional)

`install.sh` creates `config.json`; the defaults are fine for almost everyone.

| Key | Default | What it does |
|-----|---------|--------------|
| `port` | `8080` | Dashboard port |
| `usage_poll_seconds` | `120` | How often to ask Anthropic for usage |
| `battery_poll_seconds` | `20` | How often to read the PiSugar |
| `credentials_path` | `null` | Custom credentials location (auto-detected otherwise) |
| `night_start` | `"22:00"` | When the screen dims and Pip goes to sleep |
| `night_end` | `"07:00"` | When Pip wakes up |
| `hat_display` | `true` | Draw on the Whisplay HAT LCD (auto-off when absent) |

## Whisplay HAT screen

Using the [PiSugar Whisplay HAT](https://github.com/PiSugar/whisplay)?
Nothing to configure: `install.sh` installs the three apt libraries it
needs (`python3-pil`, `python3-spidev`, `python3-gpiozero`), enables SPI,
and the tracker draws straight onto the LCD — clock, battery, Pip, and the
top three usage meters, in the same day/night themes. If no HAT is
attached the display thread just switches itself off.

Two notes:

- Don't run PiSugar's own Whisplay demo/daemon at the same time — two
  programs fighting over one screen ends badly for both.
- Set `"hat_display": false` in `config.json` if you ever want it off.

## Kiosk mode (optional, for HDMI screens)

Got a regular screen on the Pi? Show the dashboard full-screen on boot:

```bash
sudo apt install -y chromium-browser
chromium-browser --kiosk --app=http://localhost:8080
```

The layout compacts itself to fit the common small panels (800×480 and
480×320) with no scrolling. Heads-up: Chromium is heavy for the Zero 2 W's
512 MB of RAM — it works, but give it a minute to start. Checking from
your phone's browser is always the snappy option.

## How it works

- `app/anthropic_usage.py` calls the same usage endpoint Claude Code's
  `/usage` command uses (`api.anthropic.com/api/oauth/usage`) with your
  OAuth token, and refreshes the token when it expires. Real percentages
  from Anthropic — no token counting or estimating.
- `app/pisugar.py` talks to `pisugar-server` on `127.0.0.1:8423`.
- `app/display.py` drives the Whisplay's ST7789 LCD over SPI directly
  (same pins and init sequence as PiSugar's own driver), rendering frames
  with Pillow.
- `app/main.py` polls both in the background and serves the dashboard plus
  a small `/api/status` JSON endpoint.
- "Session detected" means usage went up between two polls; Pip keeps
  dancing for 15 minutes after the last increase, then goes back to
  chilling.
- Day/night uses the browser's clock. Fonts are Claude's brand stacks with
  safe fallbacks (Georgia / system sans), so nothing is downloaded and
  nothing needs a license.

## Custom mascot art (optional)

Like Pip but want fancier artwork? Drop five transparent PNGs named
`happy.png`, `chill.png`, `worried.png`, `panic.png`, `sleep.png` into
`app/web/img/pip/` and restart — both the web dashboard and the HAT LCD
switch to them automatically (the built-in vector Pip stays as fallback if
any are missing). `scripts/slice-mascot.py` cuts a generated character
sheet into individual transparent sprites for you.

## Troubleshooting

```bash
journalctl -u claude-tracker -f    # live logs
systemctl restart claude-tracker   # restart after editing config.json
```

The dashboard itself surfaces the most common problems (no credentials yet,
expired sign-in, Anthropic unreachable) in a banner at the top.
