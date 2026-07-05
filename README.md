# ClaudeTrackerPi

A tiny always-on dashboard for your **Claude usage limits**, built for a
**Raspberry Pi Zero 2 W** with a **PiSugar** battery. It shows:

- **Meters and percentages** for every usage window Claude reports
  (5-hour session, weekly, weekly Opus, …) — fuel-gauge style, so the bar
  shows how much you have **left**
- **When each limit resets** — live countdown plus local clock time
- **PiSugar battery level** (with a charging indicator)
- A **dancing Claude mascot** who parties while you have plenty of usage,
  gets sweaty when you're running low, and panics when you're almost out

No pip installs, no Node, no database — one Python 3 standard-library process
serving one page. It runs happily in a few MB of RAM on the Zero 2 W, and you
view it from any browser on your network (phone, laptop, or a screen attached
to the Pi in kiosk mode).

## Setup (two steps)

### 1. On the Pi

```bash
git clone https://github.com/DaveEuson/ClaudeTrackerPi.git
cd ClaudeTrackerPi
./install.sh
```

That creates a `claude-tracker` systemd service that starts on boot. The
installer prints your dashboard URL, e.g. `http://raspberrypi.local:8080`.

### 2. On the computer where you use Claude Code

The tracker reads your limits with the same sign-in Claude Code uses. Copy
your credentials to the Pi with the helper script (works on macOS and Linux):

```bash
./scripts/send-credentials.sh pi@raspberrypi.local
```

That's it. Within a minute or two the meters go live. The tracker refreshes
its token automatically from then on, so this is normally a one-time step.
(If you ever log out of Claude Code or the sign-in expires, the dashboard
tells you and you just re-run the script.)

> Already run Claude Code on the Pi itself? Then skip step 2 — the tracker
> finds `~/.claude/.credentials.json` automatically.

## Try it without a Pi

```bash
python3 app/main.py --demo
```

Serves fake data on http://localhost:8080 so you can see the dashboard
(and the dance moves) from any machine.

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

## Kiosk mode (optional)

Got a little screen on the Pi? Show the dashboard full-screen on boot:

```bash
sudo apt install -y chromium-browser
chromium-browser --kiosk --app=http://localhost:8080
```

## How it works

- `app/anthropic_usage.py` calls the same usage endpoint Claude Code's
  `/usage` command uses (`api.anthropic.com/api/oauth/usage`) with your
  OAuth token, and refreshes the token when it expires.
- `app/pisugar.py` talks to `pisugar-server` on `127.0.0.1:8423`.
- `app/main.py` polls both in the background and serves the dashboard plus
  a small `/api/status` JSON endpoint.
- The meters turn amber under 30% left and red under 10% — same for the
  mascot's mood.

## Troubleshooting

```bash
journalctl -u claude-tracker -f    # live logs
systemctl restart claude-tracker   # restart after editing config.json
```

The dashboard itself surfaces the most common problems (no credentials yet,
expired sign-in, Anthropic unreachable) in a banner at the top.
