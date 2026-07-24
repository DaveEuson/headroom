# Headroom Mini

A tiny desk gadget that shows your **Claude usage limits** at a glance — how
much you have left in each window, when it resets, and a phone alert when you're
running low. No terminal, no menubar, no estimating.

Built on a **~$26 [Waveshare ESP32-S3-Touch-LCD-2](https://www.waveshare.com/esp32-s3-touch-lcd-2.htm)** —
one board with the screen, touch, battery header, and USB-C all on it. No
Raspberry Pi, no Linux, no soldering.

## Get one running

No tools, no command line:

1. **Flash it in your browser.** Open **https://daveeuson.github.io/headroom/**
   in Chrome or Edge, plug the board in over USB-C, and click
   **Connect & Install**.
2. **Set Wi-Fi in the same window** — it hands the board your network over the
   same USB cable (Improv). No hotspot, no typing an address.
3. **See your usage.** Download the companion app from that page and open it —
   it finds the board on your network and feeds it your real usage. Or make the
   board self-contained (below).

## How it gets your usage

- **Companion app (easiest).** A small app on the computer where you use Claude
  Code. It reuses your existing Claude login to read your **real** numbers — the
  same ones `claude /usage` shows — and pushes them to the board. Double-click
  and forget: it auto-finds the board and starts with your computer. (It never
  does a fresh sign-in, so it avoids the throttle that blocks third-party
  logins.)
- **Self-contained (no computer).** Run the companion once with `--pair` and it
  hands the board your login; the board then polls Anthropic directly and
  refreshes its own token — nothing runs on your computer afterward. Use a
  **spare Claude account** for the board so it doesn't rotate your main login's
  token.

## What it does

- **Meters** for every usage window Claude reports (5-hour session, weekly,
  weekly Opus…), fuel-gauge style — amber under 30% left, red under 10%.
- **Reset countdowns** and a clock.
- **Three screens**, cycled by a tap: all meters → one big focus meter → a
  usage-history graph (kept in flash across reboots).
- **Touch & motion** — tap to cycle screens, long-press to flip % left / %
  used, swipe for brightness; flip it face-down to sleep, shake to wake.
- **Battery gauge** from the LiPo header.
- **Phone alerts** via ntfy or Pushover when a window crosses a threshold, with
  a recovery notice.

## Repo layout

- **`firmware/`** — the ESP32 firmware (PlatformIO). Board pinout, day-1
  runbook, and roadmap in [`firmware/README.md`](firmware/README.md).
- **`companion/`** — the desktop app that feeds the board. See
  [`companion/README.md`](companion/README.md).
- **`docs/`** — the browser-flasher setup page (served by GitHub Pages) and the
  release checklist ([`docs/RELEASE.md`](docs/RELEASE.md)).

## Build from source (developers only)

Buyers never need this — they use the browser flasher above. To change the
firmware: install [VS Code](https://code.visualstudio.com/) + the **PlatformIO**
extension, open the `firmware/` folder, and hit **Upload**. Full runbook in
[`firmware/README.md`](firmware/README.md).

## The Raspberry Pi version

The original, deluxe build — a Raspberry Pi Zero 2 W with a full web dashboard
and the "Pip" mascot — lives in its own repo, **HeadroomZero**. This repo is the
self-contained ESP32 appliance.
