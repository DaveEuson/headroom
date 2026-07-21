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
   (password `claudepi`), open `http://192.168.4.1`, enter your home Wi-Fi.
   The board reboots and shows its address.
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

Panel: ST7789, 240×320, IPS, rotation 0 = portrait.

## Notes

- The header clock's timezone is a `TZ` string in `src/main.cpp`
  (default US Eastern). Countdowns are timezone-independent.
- Touch, battery gauge, and on-device Anthropic polling are not in v0.
- If the saved Wi-Fi can't be reached at boot it falls back to the setup
  hotspot without erasing the saved network (a router reboot won't force
  reprovisioning — power-cycle the board once the router is back).
