# Headroom

A tiny always-on dashboard for your **Claude usage limits**, built for a
**Raspberry Pi Zero 2 W** with a **PiSugar** battery. Glance at it and know
how much usage you have left, when it resets, and whether Claude is being
used right now — no terminal, no menubar, no estimating.

<p align="center">
  <img src="docs/screenshots/device.jpg" width="360" alt="The assembled tracker running in its 3D-printed case, night mode, Pip asleep">
  <br>
  <em>The finished tracker — running v1.1 in the Ground&nbsp;Zero case, night mode.</em>
</p>

<p align="center">
  <img src="docs/screenshots/day.png" width="380" alt="Day mode — cream Claude theme, Pip dancing">
  &nbsp;
  <img src="docs/screenshots/night.png" width="380" alt="Night mode — dark Claude theme, Pip asleep">
</p>

## What you get

- **Meters and percentages** for every usage window Claude reports (5-hour
  session, weekly, weekly Opus, …) — fuel-gauge style, so the bar shows how
  much you have **left**; amber under 30%, red under 10%
- **Reset countdowns** — live "resets in 2h 13m" plus the local clock time,
  and a big full-screen countdown the moment a session hits 100%
- **Usage history** — a sparkline under each meter on the web, and a trend
  graph that rotates onto the LCD, so you can see how fast you're burning
- **A clock**, and Claude's own look: the warm cream claude.ai theme by day,
  its dark theme at night — or force light/dark, and switch to a 24-hour clock
- **Alerts** — an optional beep from the Whisplay speaker and/or a push to
  your phone (ntfy or Pushover) when you run out and when usage is restored
- **A settings page** (`/settings`) for all of the above — theme, clock,
  brightness + night dimming, on-screen history, and alerts — saved on the Pi
- **Scan-to-open QR** rotates onto the LCD so you can pull the dashboard up
  on your phone anytime, not just during setup
- **PiSugar battery level** with a charging indicator
- **Whisplay HAT support** — draws a compact version of the dashboard
  right on the PiSugar Whisplay's 1.69″ LCD (240×280, ST7789), no browser
  or desktop needed on the Pi
- **Companion feed** — a tiny script on the computer where you use Claude Code
  reuses its existing login to read your **real** usage numbers (same ones
  `claude /usage` shows) and pushes them to the Pi — no sign-in, so it never
  hits the throttle that blocks fresh third-party logins
- **Wi-Fi setup hotspot** — plug it in somewhere new and it can't find a
  network? It becomes its own Wi-Fi (`Headroom-Setup`) and shows a
  scan-to-join QR; a setup page pops up on your phone where you pick your
  home network
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

## Got a kit? First plug-in

If you bought a pre-assembled kit, the SD card already has everything
installed and set to start on boot — you never touch a terminal. From
power-on to live meters is about five minutes:

1. **Plug in the USB-C power.** It boots in under a minute. (The PiSugar
   battery means it's instant-on and can even run untethered.)
2. **Join it to your Wi-Fi.** On first boot it makes its own hotspot and the
   screen shows a QR code. Scan it with your phone — a setup page opens by
   itself. Pick your home network and enter the password; the screen shows
   *"Connecting…"* and hops over.
3. **The screen says "Almost there"** and shows an address. On the computer
   where you use Claude Code, open that address in a browser (e.g.
   `http://claudetracker.local:8080/setup`).
4. **Download the companion** for your OS from that page and double-click it.
   (First launch: macOS → right-click → **Open**; Windows → **More info →
   Run anyway**.) It finds the tracker on the network by itself, sends the
   first reading, and sets itself to run at every login.
5. **Done.** The QR disappears and the screen comes alive — meters, reset
   countdowns, battery, and Pip reacting to your usage. You never touch the
   companion again.

> **One requirement:** the companion reads the Claude Code login already on
> your computer, so you need **Claude Code signed in** on the machine in
> steps 3–4 (a Pro or Max plan). If you only use Claude in a browser, there's
> no login for it to read — this tracker is for Claude Code users.

## Build it yourself (two steps)

### What you'll need

| Part | Notes |
|------|-------|
| [Raspberry Pi Zero 2 W](https://www.amazon.com/dp/B0FX3SCP5F?tag=daveeuson01-20) | The brain of the tracker. |
| [Solderless GPIO hammer header](https://www.amazon.com/dp/B0CGRYYY63?tag=daveeuson01-20) **·** or a [soldering kit](https://www.amazon.com/dp/B087767KNW?tag=daveeuson01-20) | The Whisplay HAT needs a 40‑pin header on the Pi. The press‑on "hammer" header adds it with no soldering; or solder your own. Skip if your Pi already has a header. |
| [PiSugar Whisplay HAT](https://www.amazon.com/dp/B0FPG8S6K6?tag=daveeuson01-20) | The 1.69″ LCD board that sits on top. (Add a PiSugar battery separately if yours doesn't include one.) |
| [microSD card](https://www.amazon.com/dp/B08L5HMJVW?tag=daveeuson01-20) | 16–32 GB is plenty. Flash it with Raspberry Pi OS Lite. |
| [3D‑printed case](https://www.printables.com/model/1692376-ground-zero-raspberry-pi-zero-2w-whisplay-case-v1) | Optional. The "Ground Zero" enclosure, designed for the Pi Zero 2 W + Whisplay. Free STL — print it yourself or use a printing service. |

*As an Amazon Associate I earn from qualifying purchases. The 3D‑print model is a free third‑party design.*

### 1. On the Pi

```bash
git clone https://github.com/DaveEuson/Headroom.git
cd Headroom
./install.sh
```

That creates a `headroom` systemd service that starts on boot. The
installer prints your dashboard URL, e.g. `http://raspberrypi.local:8080`.

> **Set the timezone** so the clock and reset countdowns are right:
> `sudo raspi-config` → *Localisation Options → Timezone* (or
> `sudo timedatectl set-timezone America/New_York`). A headless-flashed
> card often defaults to UTC.

### 2. Install the companion on your computer

The Pi can't sign in directly (Anthropic throttles fresh third-party logins).
Instead, a tiny **companion app** runs on the computer where you already use
Claude Code, reuses that existing login to read your real usage numbers, and
pushes them to the Pi. No new sign-in, so it never hits the throttle.

Until it's connected, the tracker's screen shows a **QR code**. Scan it (or open
`http://<tracker-address>:8080/setup`) to reach a page with one-click downloads
for **Windows, macOS, and Linux**. Download the one for your computer and
double-click it — it **finds the tracker on your network by itself**, sends the
first reading, and **sets itself to run at every login**. Nothing to type.

> Prefer the script? With Python 3 installed, run
> `python3 companion/companion.py` — same auto-discovery, no download.

Full details, tuning, and how the double-click apps are built:
**[companion/README.md](companion/README.md)**.

### Moving the Pi to a new network?

If the Pi boots and can't find a known Wi-Fi network for ~45 seconds, the
screen switches to **Wi-Fi setup**: scan the QR to join its
`Headroom-Setup` hotspot, and a page opens on your phone to pick your
network and enter the password. Once it hops over, the dashboard takes over.
(Requires NetworkManager — standard on Raspberry Pi OS.)

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

You rarely need to touch `config.json` — most things live on the **Settings
page** below.

## Settings

Open **`http://<your-pi>:8080/settings`** (or tap *Settings* in the dashboard
footer). Changes save to the Pi instantly and apply to both the screen and the
web dashboard:

- **Appearance** — theme (auto / always light / always dark), meters showing
  **% left or % used**, 24-hour clock, screen **brightness**, **dim at night**,
  and the **retro CRT scanlines**.
- **Usage history on screen** — toggle the rotating trend graph on the LCD, and
  set how often the **phone QR** appears (or turn it off).
- **Alerts** — a **speaker beep** and/or a **phone push** when you run out of
  session usage and when it's restored:
  - **ntfy** (recommended, free, no account): pick a hard-to-guess topic and
    subscribe to it in the [ntfy app](https://ntfy.sh/).
  - **Pushover**: register your own app and paste your token + user key.
  - **Send test** fires a sample alert so you can confirm it works.
  Audio is **off by default**; to hear it you need ALSA (`sudo apt install
  alsa-utils`) and the Whisplay's WM8960 audio driver set up.

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
journalctl -u headroom -f    # live logs
systemctl restart headroom   # restart after editing config.json
```

The dashboard itself surfaces the most common problems (no credentials yet,
expired sign-in, Anthropic unreachable) in a banner at the top.
