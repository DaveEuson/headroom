# Headroom Mini — ESP32-S3 firmware

Headroom on a **Waveshare ESP32-S3-Touch-LCD-2** (2" ST7789 240×320 IPS,
ESP32-S3R8, battery header). ~$26, no Raspberry Pi, no Linux.

**v0 scope:** the board joins your Wi-Fi (first boot: its own
`Headroom-Setup` hotspot with a phone setup page, same flow as the Pi), then
speaks the **same HTTP API as the Pi tracker** — `GET /api/status` with the
`"app": "Headroom"` discovery marker and `POST /api/push` — so the existing
desktop **companion feeds it unchanged**. Claude-night-theme meters, reset
countdowns, NTP clock.

**Self-contained (no companion):** run the companion **once** with
`companion.py --pair` (it finds the board automatically) — it hands the board
your existing Claude Code login, no copying by hand, and the board then polls
Anthropic's usage endpoint directly and refreshes its own token. Nothing runs
on your computer afterward. (Manual fallback if you can't run the companion:
paste the login at `http://<board-ip>:8080/connect`.) Use a **separate Claude
login for the board** (a spare account), or it and your computer's Claude Code
will keep rotating each other's refresh token and logging each other out.

## Easiest: the browser flasher (no tools)

For anyone who just wants it working — no VS Code, no PlatformIO, no git —
open the setup page in **Chrome or Edge on a computer** and click
**Connect & Install**:

> **https://daveeuson.github.io/HeadroomMini/**

It flashes the latest release straight from the browser, then walks through
Wi-Fi and the companion download. That page is `docs/index.html` here (served
by GitHub Pages), and it flashes the `headroom-mini-merged.bin` that a tagged
release builds. The developer flow below is only needed to change the firmware.

## Developer flow (build from source)

1. Install [VS Code](https://code.visualstudio.com/) + the **PlatformIO IDE**
   extension (or `pip install platformio` for the CLI).
2. Plug the board in over USB-C (**use a data cable**, not a charge-only one).
3. From this `firmware/` folder:

   ```
   pio run -t upload
   pio device monitor        # optional: serial logs at 115200
   ```

   If the upload can't find the port, hold **BOOT**, tap **RESET**, release
   BOOT, retry (classic ESP32 bootloader dance).

4. On the screen: join the `Headroom-Setup` Wi-Fi from your phone
   (password `headroom`), open `http://192.168.4.1`, then **pick your home
   network from the scanned list** and type its password (or type the name by
   hand if it's hidden). The board reboots and shows its address.
5. Feed it from your computer:

   ```
   python3 companion/companion.py --pi http://<board-ip>:8080 --no-install
   ```

   (`--no-install` so it doesn't fight your Pi tracker's autostart; drop the
   flag if this becomes your only tracker. `headroom.local` also works if
   your OS resolves mDNS.)

Within a couple of minutes the meters go live.

## Board pinout (for reference)

| Function | GPIO |
|---|---|
| LCD SCLK / MOSI / MISO | 39 / 38 / 40 |
| LCD DC / CS / Backlight | 42 / 45 / 1 |
| LCD RST | — (soft reset) |
| Touch (CST816D, I2C 0x15) | SDA 48 / SCL 47 |

Panel: ST7789, 240×320, IPS, rotation 2 = portrait with the USB-C connector on
top (flip to 0 in `src/main.cpp` if you mount it the other way up).

## Roadmap

- **v0 (this)** — screen + Wi-Fi + companion-fed meters. Bring-up day.
- **Phase 1.5 — touch & motion (done).** CST816D capacitive touch + QMI8658
  IMU on the shared I2C bus (SDA 48 / SCL 47):
  - **Tap / swipe L-R** → cycle screens (all meters ↔ big focus meter)
  - **Long-press** → toggle % left / % used (saved)
  - **Swipe up / down** → brightness
  - **Face-down** → screen off; **face-up / shake** → wake
  - **BOOT held ~5s** → factory-reset Wi-Fi + login
  - Both sensors degrade gracefully — if a chip isn't found its feature is
    simply off. IMU auto-rotate isn't wired yet (mounting-dependent).
- **Phase 2 — self-contained (done).** Polls Anthropic's usage endpoint
  on-device: paste a login once at `/connect`, token refreshed on-device, no
  companion. TLS currently runs without cert pinning (`setInsecure`) — fine on
  a trusted home network; pin a CA bundle before shipping if that matters.
- **Phase 3 — polish (done).** Battery gauge (VBAT on GPIO5 via the 200K/100K
  divider) shown on every screen; usage history stored in flash with a graph
  screen; and phone push alerts — open `http://<board-ip>/alerts`, enter an
  **ntfy** topic (or Pushover keys) and a threshold, and the board pushes when
  a window crosses it (with a recovery notice). Charge state is inferred from
  voltage (no charge-status line on this board).

## Notes

- The header clock's timezone is a `TZ` string in `src/main.cpp`
  (default US Eastern). Countdowns are timezone-independent.
- Touch, battery gauge, and on-device Anthropic polling are not in v0.
- If the saved Wi-Fi can't be reached at boot it falls back to the setup
  hotspot without erasing the saved network (a router reboot won't force
  reprovisioning — power-cycle the board once the router is back).
