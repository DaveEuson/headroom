# Headroom Mini — ESP32-S3 firmware

Headroom on a **Waveshare ESP32-S3-Touch-LCD-2** (2" ST7789 240×320 IPS,
ESP32-S3R8, battery header). ~$26, no Raspberry Pi, no Linux.

**v0 scope:** the board joins your Wi-Fi (first boot: its own
`Headroom-Setup` hotspot with a phone setup page, same flow as the Pi), then
speaks the **same HTTP API as the Pi tracker** — `GET /api/status` with the
`"app": "Headroom"` discovery marker and `POST /api/push` — so the existing
desktop **companion feeds it unchanged**. Claude-night-theme meters, reset
countdowns, NTP clock.

**Phase 2 (planned):** poll Anthropic's usage endpoint directly on-device
(token pasted once via the web page, refreshed on-device) — no companion at
all. Needs its own dedicated Claude login to avoid refresh-token rotation
clashes with your computer's Claude Code.

## Day-1 quickstart

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
- **Phase 1.5 — touch & motion.** The hardware's already there (CST816D
  capacitive touch on I2C 48/47, plus a 6-axis IMU):
  - **Tap** → cycle screens: meters → history graph → phone QR
  - **Long-press** → toggle % left / % used, brightness
  - **IMU auto-rotate** → portrait/landscape follows how it sits on the desk
  - **Face-down to dim**, shake to wake
  - **BOOT button held 5s** → factory-reset Wi-Fi
- **Phase 2 — self-contained.** Poll Anthropic's usage endpoint on-device:
  paste a dedicated credential once via the device's web page, token
  refreshed on-device (needs its own Claude login to avoid refresh-token
  rotation clashes with your computer's Claude Code). No companion at all.
- **Phase 3 — polish.** Battery gauge from the LiPo header, usage history
  stored in flash, alerts (the board has no speaker, but phone push works
  from anywhere).

## Notes

- The header clock's timezone is a `TZ` string in `src/main.cpp`
  (default US Eastern). Countdowns are timezone-independent.
- Touch, battery gauge, and on-device Anthropic polling are not in v0.
- If the saved Wi-Fi can't be reached at boot it falls back to the setup
  hotspot without erasing the saved network (a router reboot won't force
  reprovisioning — power-cycle the board once the router is back).
