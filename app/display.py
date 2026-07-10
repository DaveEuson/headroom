"""Render the tracker on the PiSugar Whisplay HAT's 1.69" LCD (240x280).

The Whisplay's screen is an ST7789 on SPI0 (DC=GPIO27, RST=GPIO4,
backlight=GPIO22) -- no framebuffer, no browser. We draw a compact
dashboard with Pillow and push RGB565 frames over SPI, mirroring the
init sequence from PiSugar's own driver (github.com/PiSugar/whisplay).

Needs: python3-pil, python3-spidev, python3-gpiozero (apt packages,
installed by install.sh). If any are missing, or there is no SPI
device, we log one line and let the web dashboard carry on alone.
"""

import datetime
import os
import sys
import time

SPRITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "web", "img", "pip")
SPRITE_BOX = (150, 76)   # sprites are scaled to fit this area on the LCD

WIDTH, HEIGHT = 240, 280
Y_OFFSET = 20            # the 240x280 panel sits 20 rows into 240x320 RAM
MADCTL = 0xC0            # rotation used by the vendor driver
SPI_HZ = 40_000_000

DC_PIN, RST_PIN, BL_PIN = 27, 4, 22

ACTIVE_SECONDS = 900  # keep in sync with main.ACTIVE_SECONDS

# compact labels sized for a 1.69" screen
SHORT_LABELS = {
    "five_hour": "Session (5h)",
    "seven_day": "Weekly",
    "seven_day_sonnet": "Sonnet",
    "seven_day_opus": "Opus",
    "seven_day_oauth_apps": "Apps",
}

THEMES = {
    "day": {
        "bg": (240, 238, 230), "ink": (61, 57, 41), "muted": (138, 132, 120),
        "case": (230, 218, 191), "case_edge": (185, 166, 126),
        "accent": (217, 119, 87), "accent_track": (235, 217, 206),
        "warn": (232, 163, 23), "warn_track": (240, 227, 195),
        "crit": (196, 69, 61), "crit_track": (239, 215, 211),
    },
    "night": {
        "bg": (38, 38, 36), "ink": (245, 244, 239), "muted": (148, 144, 126),
        "case": (230, 218, 191), "case_edge": (185, 166, 126),
        "accent": (217, 119, 87), "accent_track": (74, 56, 47),
        "warn": (250, 178, 25), "warn_track": (70, 59, 26),
        "crit": (224, 82, 82), "crit_track": (74, 39, 39),
    },
}
SCREEN_DARK = (32, 32, 30)
PHOSPHOR = {"ok": (76, 217, 123), "warn": (250, 178, 25), "crit": (255, 111, 97)}
SWEAT = (124, 196, 250)
CAP, CAP_BAND, POMPOM = (109, 91, 208), (133, 119, 224), (242, 239, 233)


def _to_rgb565(image):
    """Pack a PIL image into big-endian RGB565 bytes for the ST7789.

    Uses numpy when available (fast); falls back to pure Python so the
    display still works if numpy isn't installed.
    """
    image = image.convert("RGB")
    try:
        import numpy as np

        arr = np.asarray(image, dtype=np.uint16)
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return rgb565.astype(">u2").tobytes()   # big-endian, high byte first
    except ImportError:
        rgb = image.tobytes()
        out = bytearray((len(rgb) // 3) * 2)
        for i in range(0, len(rgb), 3):
            v = (((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3)
                 | (rgb[i + 2] >> 3))
            j = (i // 3) * 2
            out[j] = v >> 8
            out[j + 1] = v & 0xFF
        return bytes(out)


class ST7789:
    """Minimal driver for the Whisplay's LCD, matching the vendor init."""

    def __init__(self):
        import spidev
        from gpiozero import DigitalOutputDevice

        self.dc = DigitalOutputDevice(DC_PIN)
        self.rst = DigitalOutputDevice(RST_PIN, initial_value=True)
        # Whisplay backlight is ACTIVE-LOW (pin LOW = lit). Prefer PWM so we
        # can dim; fall back to plain on/off if PWM isn't available.
        self._pwm_backlight = False
        try:
            from gpiozero import PWMOutputDevice
            self.backlight = PWMOutputDevice(
                BL_PIN, active_high=False, initial_value=1.0, frequency=1000)
            self._pwm_backlight = True
        except Exception:
            self.backlight = DigitalOutputDevice(
                BL_PIN, active_high=False, initial_value=True)
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = SPI_HZ
        self.spi.mode = 0
        self._init_panel()

    def set_brightness(self, level):
        """level 0.0-1.0. With PWM this dims; without, it's on/off."""
        level = max(0.0, min(1.0, float(level)))
        if self._pwm_backlight:
            self.backlight.value = level
        elif level <= 0.02:
            self.backlight.off()
        else:
            self.backlight.on()

    def _cmd(self, command, *params):
        self.dc.off()
        self.spi.writebytes([command])
        if params:
            self.dc.on()
            self.spi.writebytes(list(params))

    def _init_panel(self):
        self.rst.off(); time.sleep(0.02); self.rst.on(); time.sleep(0.12)
        self._cmd(0x11)                     # sleep out
        time.sleep(0.12)
        self._cmd(0x36, MADCTL)             # orientation
        self._cmd(0x3A, 0x05)               # 16-bit color
        self._cmd(0xB2, 0x0C, 0x0C, 0x00, 0x33, 0x33)
        self._cmd(0xB7, 0x35)
        self._cmd(0xBB, 0x32)
        self._cmd(0xC2, 0x01)
        self._cmd(0xC3, 0x15)
        self._cmd(0xC4, 0x20)
        self._cmd(0xC6, 0x0F)
        self._cmd(0xD0, 0xA4, 0xA1)
        self._cmd(0xE0, 0xD0, 0x08, 0x0E, 0x09, 0x09, 0x05, 0x31, 0x33,
                  0x48, 0x17, 0x14, 0x15, 0x31, 0x34)
        self._cmd(0xE1, 0xD0, 0x08, 0x0E, 0x09, 0x09, 0x15, 0x31, 0x33,
                  0x48, 0x17, 0x14, 0x15, 0x31, 0x34)
        self._cmd(0x21)                     # inversion on (panel needs it)
        self._cmd(0x29)                     # display on

    def show(self, image):
        y0, y1 = Y_OFFSET, Y_OFFSET + HEIGHT - 1
        self._cmd(0x2A, 0, 0, (WIDTH - 1) >> 8, (WIDTH - 1) & 0xFF)
        self._cmd(0x2B, y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF)
        self._cmd(0x2C)
        buf = _to_rgb565(image)
        self.dc.on()
        try:
            self.spi.writebytes2(buf)
        except AttributeError:
            for i in range(0, len(buf), 4096):
                self.spi.writebytes(list(buf[i:i + 4096]))


# ---------------------------------------------------------------- rendering

def _load_sprites():
    """Load custom mascot art (app/web/img/pip/*.png) if the user added it."""
    from PIL import Image

    sprites = {}
    for mood in ("happy", "chill", "worried", "panic", "sleep"):
        path = os.path.join(SPRITE_DIR, f"{mood}.png")
        if not os.path.isfile(path):
            continue
        try:
            img = Image.open(path).convert("RGBA")
            img.thumbnail(SPRITE_BOX, Image.LANCZOS)
            sprites[mood] = img
        except OSError:
            pass
    return sprites


def _load_fonts():
    from PIL import ImageFont
    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf"
    fonts = {}
    try:
        fonts["huge"] = ImageFont.truetype(path % "-Bold", 46)
        fonts["clock"] = ImageFont.truetype(path % "-Bold", 28)
        fonts["big"] = ImageFont.truetype(path % "-Bold", 17)
        fonts["label"] = ImageFont.truetype(path % "", 14)
        fonts["small"] = ImageFont.truetype(path % "", 12)
    except OSError:
        default = ImageFont.load_default()
        fonts = {k: default for k in
                 ("huge", "clock", "big", "label", "small")}
    return fonts


def _parse_hm(text, fallback):
    try:
        h, m = str(text).split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except (ValueError, AttributeError):
        return fallback


def _is_night(snapshot):
    night = snapshot.get("night") or {}
    start = _parse_hm(night.get("start"), 22 * 60)
    end = _parse_hm(night.get("end"), 7 * 60)
    now = datetime.datetime.now()
    mins = now.hour * 60 + now.minute
    if start == end:
        return False
    return (start <= mins or mins < end) if start > end \
        else (start <= mins < end)


def _target_brightness(snapshot):
    """Backlight level 0.0-1.0 from the brightness setting, dimmed at night."""
    try:
        b = max(0, min(100, int(snapshot.get("brightness", 100))))
    except (TypeError, ValueError):
        b = 100
    if snapshot.get("night_dim", True) and _is_night(snapshot):
        b = min(b, 15)          # gentle night glow
    return b / 100.0


def _theme_for(snapshot, night):
    """Colors follow the 'theme' setting; 'auto' tracks the day/night schedule.
    (This is separate from `night`, which decides whether the mascot sleeps.)"""
    pref = (snapshot.get("theme") or "auto").lower()
    if pref == "dark":
        return THEMES["night"]
    if pref == "light":
        return THEMES["day"]
    return THEMES["night" if night else "day"]


def _mood(snapshot, night):
    windows = snapshot.get("windows") or []
    if night:
        return "sleep"
    if windows:
        least = min(100 - w["utilization"] for w in windows)
        if least <= 10:
            return "panic"
        if least <= 30:
            return "worried"
    return "happy" if snapshot.get("session_active") else "chill"


def _reset_text(resets_at):
    if not resets_at:
        return ""
    try:
        when = datetime.datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = when - datetime.datetime.now(datetime.timezone.utc)
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "resetting..."
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {mins}m"
    return f"resets in {mins}m"


def _seconds_until(resets_at):
    if not resets_at:
        return None
    try:
        when = datetime.datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((when - datetime.datetime.now(
        datetime.timezone.utc)).total_seconds())


def _countdown_hms(resets_at):
    """Big live 'H:MM:SS' (or 'Dd HH:MM' past a day) countdown, or None."""
    secs = _seconds_until(resets_at)
    if secs is None:
        return None
    if secs <= 0:
        return "0:00:00"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, s = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{mins:02d}"
    return f"{hours}:{mins:02d}:{s:02d}"


def _session_window(snapshot):
    for w in (snapshot.get("windows") or []):
        if w.get("key") == "five_hour":
            return w
    return None


def _draw_pip(draw, mood, frame, theme):
    """A simplified Pip (the retro computer) centered around x=120."""
    bounce = -3 if (mood in ("happy", "panic") and frame % 2) else 0
    dy = bounce

    # legs
    for x in (101, 134):
        draw.rounded_rectangle((x, 122 + dy, x + 5, 138), 2,
                               fill=theme["case_edge"])
    # arms (up when dancing, down otherwise)
    ay = (62, 76) if (mood == "happy" and frame % 2) or mood == "panic" \
        else (92, 78)
    draw.line((80, 78 + dy, 64, ay[0] + dy), fill=theme["case_edge"], width=4)
    draw.line((160, 78 + dy, 176, ay[0] + dy), fill=theme["case_edge"], width=4)
    # monitor + screen
    draw.rounded_rectangle((78, 50 + dy, 162, 106 + dy), 6,
                           fill=theme["case"], outline=theme["case_edge"])
    draw.rounded_rectangle((86, 58 + dy, 154, 96 + dy), 3, fill=SCREEN_DARK)
    # rainbow stripe chip
    for i, color in enumerate(((67, 204, 110), (250, 178, 25),
                               (232, 136, 58), (224, 82, 82))):
        draw.rectangle((88 + i * 8, 99 + dy, 94 + i * 8, 102 + dy), fill=color)
    # keyboard
    draw.rounded_rectangle((70, 110 + dy, 170, 122 + dy), 4,
                           fill=theme["case"], outline=theme["case_edge"])

    phos = PHOSPHOR["warn" if mood == "worried" else
                    "crit" if mood == "panic" else "ok"]
    cx, cy = 120, 74 + dy
    if mood == "chill":         # sunglasses screensaver
        draw.rounded_rectangle((cx - 18, cy - 6, cx - 4, cy + 2), 2, fill=phos)
        draw.rounded_rectangle((cx + 4, cy - 6, cx + 18, cy + 2), 2, fill=phos)
        draw.rectangle((cx - 5, cy - 4, cx + 5, cy - 2), fill=phos)
        draw.arc((cx - 9, cy + 2, cx + 9, cy + 14), 20, 160, fill=phos, width=2)
    elif mood == "happy":       # code flies across the screen
        widths = (28, 40, 20, 36, 26, 16)
        for i, w in enumerate(widths[:4 + frame % 3]):
            x = 90 + (6 if i % 3 else 0)
            draw.rectangle((x, 61 + dy + i * 6, x + w, 63 + dy + i * 6),
                           fill=phos)
    elif mood == "sleep":       # crescent moon + nightcap + Zzz
        draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=(63, 143, 92))
        draw.ellipse((cx - 5, cy - 13, cx + 13, cy + 5), fill=SCREEN_DARK)
        draw.polygon((82, 52, 108, 32, 148, 38, 150, 52), fill=CAP)
        draw.rounded_rectangle((76, 46, 158, 56), 5, fill=CAP_BAND)
        draw.ellipse((146, 32, 158, 44), fill=POMPOM)
        if frame % 2:
            draw.text((164, 44), "z", fill=theme["muted"])
            draw.text((172, 34), "Z", fill=theme["muted"])
    else:                       # worried / panic face
        draw.rectangle((cx - 14, cy - 8, cx - 9, cy), fill=phos)
        draw.rectangle((cx + 9, cy - 8, cx + 14, cy), fill=phos)
        if mood == "panic":
            draw.ellipse((cx - 4, cy + 5, cx + 4, cy + 13), fill=phos)
        else:
            draw.rectangle((cx - 7, cy + 8, cx + 7, cy + 10), fill=phos)
    if mood in ("worried", "panic") and frame % 2:
        draw.ellipse((166, 58 + dy, 172, 67 + dy), fill=SWEAT)


_SETUP_HINTS = ("credential", "sign-in", "sign in", "log in", "login",
                "access token", "re-copy", "not logged in", "not connected",
                "companion", "waiting for")

WIFI_STATE_FILE = "/run/claude-tracker/wifi.json"


def _wifi_setup_state():
    """First-boot Wi-Fi provisioning state written by wifi_setup.py."""
    import json as _json
    try:
        with open(WIFI_STATE_FILE, encoding="utf-8") as fh:
            return _json.load(fh)
    except (OSError, ValueError):
        return None


def _needs_setup(snapshot):
    """True when the tracker has no working Claude login yet."""
    if snapshot.get("windows"):
        return False
    err = (snapshot.get("usage_error") or "").lower()
    return any(hint in err for hint in _SETUP_HINTS)


def _lan_ip():
    """Best-effort LAN IP of this Pi (no packets are actually sent)."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None


def _local_url(config, path=""):
    """URL of this Pi's dashboard, for the setup QR code."""
    port = int((config or {}).get("port", 8080))
    ip = _lan_ip()
    if ip:
        return f"http://{ip}:{port}{path}"
    import socket
    try:
        return f"http://{socket.gethostname()}.local:{port}{path}"
    except OSError:
        return None


def _qr_image(data, target=150):
    """Render `data` as a QR code PIL image ~target px wide, or None."""
    try:
        import qrcode
    except ImportError:
        return None
    try:
        from PIL import Image
        qr = qrcode.QRCode(border=2, box_size=1,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color=(15, 15, 15),
                            back_color=(255, 255, 255)).convert("RGB")
    except Exception:
        return None
    if img.width and img.width < target:
        scale = max(1, target // img.width)
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    return img


def _center(draw, text, y, font, fill):
    w = draw.textlength(text, font=font)
    draw.text(((WIDTH - w) / 2, y), text, font=font, fill=fill)


def _render_wifi_setup(theme, fonts, wifi):
    """First-boot screen: QR that joins the Pi's own setup hotspot."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    if wifi.get("joining"):
        _center(draw, "Connecting to", 96, fonts["big"], theme["ink"])
        _center(draw, str(wifi["joining"])[:22], 122, fonts["big"],
                theme["accent"])
        _center(draw, "watch this screen...", 152, fonts["small"],
                theme["muted"])
        return img
    _center(draw, "Wi-Fi setup", 10, fonts["clock"], theme["ink"])
    _center(draw, "Scan to join me, then a", 50, fonts["small"],
            theme["muted"])
    _center(draw, "setup page opens on your phone", 65, fonts["small"],
            theme["muted"])
    ssid = wifi.get("ssid", "ClaudeTracker-Setup")
    psk = wifi.get("password", "")
    qr = _qr_image(f"WIFI:T:WPA;S:{ssid};P:{psk};;", 140)
    if qr:
        img.paste(qr, ((WIDTH - qr.width) // 2, 82))
        y = 82 + qr.height + 6
    else:
        y = 150
    _center(draw, f"Wi-Fi: {ssid}", y, fonts["small"], theme["ink"])
    _center(draw, f"password: {psk}", y + 16, fonts["small"], theme["ink"])
    if wifi.get("error"):
        _center(draw, "last try failed - password?", y + 34, fonts["small"],
                theme["crit"])
    return img


def _render_setup(theme, fonts, url):
    """First-run screen: how to connect your Claude account from your computer."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    _center(draw, "Almost there", 12, fonts["clock"], theme["ink"])
    if not url:
        _center(draw, "connect the Pi to Wi-Fi first", 120, fonts["label"],
                theme["muted"])
        return img
    _center(draw, "On your computer, open:", 48, fonts["small"], theme["muted"])
    _center(draw, url.replace("http://", ""), 64, fonts["label"],
            theme["accent"])
    qr = _qr_image(url, 128)
    if qr:
        img.paste(qr, ((WIDTH - qr.width) // 2, 92))
        yb = 92 + qr.height + 8
    else:
        yb = 230
    _center(draw, "or scan this code", yb, fonts["small"], theme["muted"])
    return img


def _draw_header(draw, snapshot, theme, fonts):
    """Clock (left) + battery (right). Wi-Fi lives in the footer now."""
    if snapshot.get("clock_24h"):
        clock = time.strftime("%H:%M")
    else:
        clock = time.strftime("%I:%M %p").lstrip("0")
    draw.text((10, 6), clock, font=fonts["clock"], fill=theme["ink"])
    battery = snapshot.get("battery")
    if battery:
        pct = round(battery["percent"])
        label = f"{pct}%" + ("+" if battery.get("charging") else "")
        w = draw.textlength(label, font=fonts["big"])
        draw.text((230 - w, 8), label, font=fonts["big"], fill=theme["ink"])
        color = theme["crit"] if pct <= 10 else \
            theme["warn"] if pct <= 25 else theme["accent"]
        draw.rounded_rectangle((186, 30, 230, 37), 3, fill=theme["accent_track"])
        draw.rounded_rectangle((186, 30, 186 + max(3, int(44 * pct / 100)),
                                37), 3, fill=color)


def _draw_wifi_footer(draw, snapshot, theme, fonts):
    """Signal bars + network name, centered along the very bottom."""
    ssid = (snapshot.get("wifi") or {}).get("ssid")
    if not ssid:
        return
    label = str(ssid)[:20]
    lw = draw.textlength(label, font=fonts["small"])
    total = 11 + lw          # 3 signal bars (~8px) + gap + name
    bx = int((WIDTH - total) / 2)
    y = 264
    for i in range(3):
        bh = 3 + i * 2
        draw.rectangle((bx + i * 3, y + 9 - bh, bx + i * 3 + 2, y + 9),
                       fill=theme["muted"])
    draw.text((bx + 11, y), label, font=fonts["small"], fill=theme["muted"])


def _draw_mascot(img, draw, mood, frame, theme, sprites):
    sprite = (sprites or {}).get(mood)
    if sprite:
        bounce = -3 if (mood in ("happy", "panic") and frame % 2) else 0
        img.paste(sprite, (120 - sprite.width // 2,
                           80 - sprite.height // 2 + bounce), sprite)
    else:
        _draw_pip(draw, mood, frame, theme)


def _render_maxed(theme, fonts, snapshot, frame, sprites):
    """Session limit hit: a big live countdown to when it resets."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    _draw_header(draw, snapshot, theme, fonts)
    _draw_mascot(img, draw, "panic", frame, theme, sprites)
    crit = theme["crit"]
    sw = _session_window(snapshot) or {}
    _center(draw, "Session limit reached", 148, fonts["label"], crit)
    hms = _countdown_hms(sw.get("resets_at")) or "--:--"
    _center(draw, hms, 168, fonts["huge"], crit)
    _center(draw, "until your session resets", 224, fonts["small"],
            theme["muted"])
    # keep the weekly windows visible, condensed to one line
    parts = []
    for w in (snapshot.get("windows") or []):
        if w.get("key") == "five_hour":
            continue
        rem = max(0.0, min(100.0, 100 - w["utilization"]))
        lbl = SHORT_LABELS.get(w.get("key"), str(w["label"])[:10])
        parts.append(f"{lbl} {rem:.0f}%")
        if len(parts) == 2:
            break
    if parts:
        _center(draw, "   ".join(parts), 246, fonts["small"], theme["muted"])
    _draw_wifi_footer(draw, snapshot, theme, fonts)
    return img


def _render_history(theme, fonts, snapshot):
    """Rotating LCD screen: a line graph of a window's % left over time."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    _draw_header(draw, snapshot, theme, fonts)
    hist = snapshot.get("history") or {}
    key = "five_hour" if hist.get("five_hour") else next(
        (k for k, v in hist.items() if len(v) >= 2), None)
    series = hist.get(key or "", [])
    title = SHORT_LABELS.get(key, "Usage")
    x0, y0, x1, y1 = 16, 92, 224, 214
    _center(draw, f"{title} — usage left", 54, fonts["label"], theme["ink"])
    draw.line((x0, y1, x1, y1), fill=theme["muted"])   # baseline (0%)
    if len(series) < 2:
        _center(draw, "collecting history…", (y0 + y1) // 2,
                fonts["small"], theme["muted"])
        _draw_wifi_footer(draw, snapshot, theme, fonts)
        return img
    span_h = (series[-1][0] - series[0][0]) / 3600.0
    draw.text((x0, 74), f"last {span_h:.0f}h" if span_h >= 1 else "last <1h",
              font=fonts["small"], fill=theme["muted"])
    n = len(series)
    pts = []
    for i, (_ts, util) in enumerate(series):
        left = max(0.0, min(100.0, 100.0 - util))
        px = x0 + (i / (n - 1)) * (x1 - x0)
        py = y1 - (left / 100.0) * (y1 - y0)
        pts.append((px, py))
    cur = max(0.0, min(100.0, 100.0 - series[-1][1]))
    sev = "crit" if cur <= 10 else "warn" if cur <= 30 else "accent"
    draw.polygon([(x0, y1)] + pts + [(x1, y1)], fill=theme[sev + "_track"])
    draw.line(pts, fill=theme[sev], width=2)
    ex, ey = pts[-1]
    draw.ellipse((ex - 3, ey - 3, ex + 3, ey + 3), fill=theme[sev])
    _center(draw, f"{cur:.0f}% left now", y1 + 12, fonts["big"], theme["ink"])
    _draw_wifi_footer(draw, snapshot, theme, fonts)
    return img


def _render_phone_qr(theme, fonts, snapshot, url):
    """Rotating LCD screen: a scannable QR to open the dashboard on a phone."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    _draw_header(draw, snapshot, theme, fonts)
    _center(draw, "Open on your phone", 54, fonts["label"], theme["ink"])
    qr = _qr_image(url, 128) if url else None
    if qr:
        img.paste(qr, ((WIDTH - qr.width) // 2, 84))
        yb = 84 + qr.height + 10
    else:
        yb = 210
    if url:
        _center(draw, url.replace("http://", ""), yb, fonts["small"],
                theme["accent"])
    _draw_wifi_footer(draw, snapshot, theme, fonts)
    return img


def render(snapshot, frame, fonts, sprites=None, setup_url=None):
    from PIL import Image, ImageDraw

    night = _is_night(snapshot)          # drives the mascot sleeping
    theme = _theme_for(snapshot, night)  # colors: honors the theme setting
    wifi = _wifi_setup_state()
    if wifi:
        return _render_wifi_setup(theme, fonts, wifi)
    if _needs_setup(snapshot):
        return _render_setup(theme, fonts, setup_url)
    mood = _mood(snapshot, night)
    error = snapshot.get("usage_error")

    # Session fully used -> a big countdown is the only thing that matters.
    sw = _session_window(snapshot)
    if not error and sw is not None and (100 - sw.get("utilization", 0)) <= 0.5:
        return _render_maxed(theme, fonts, snapshot, frame, sprites)

    # Every ~17s, spend ~5s on an aux screen: alternate the usage-history
    # graph and a "scan me" QR so you can open the dashboard on your phone.
    if not error and frame % 34 >= 24:
        hist = snapshot.get("history") or {}
        have_hist = (snapshot.get("lcd_history", True)
                     and any(len(v) >= 2 for v in hist.values()))
        dash = None
        if setup_url:
            dash = (setup_url[:-len("/setup")]
                    if setup_url.endswith("/setup") else setup_url)
        show_history = have_hist and (dash is None or (frame // 34) % 2 == 0)
        if show_history:
            return _render_history(theme, fonts, snapshot)
        if dash:
            return _render_phone_qr(theme, fonts, snapshot, dash)
        if have_hist:
            return _render_history(theme, fonts, snapshot)

    img = Image.new("RGB", (WIDTH, HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    _draw_header(draw, snapshot, theme, fonts)
    _draw_mascot(img, draw, mood, frame, theme, sprites)

    # meters (first three windows) or the error banner
    windows = (snapshot.get("windows") or [])[:3]
    y = 122
    if error:
        words, line, lines = str(error).split(), "", []
        for word in words:
            trial = (line + " " + word).strip()
            if draw.textlength(trial, font=fonts["small"]) > 216:
                lines.append(line); line = word
            else:
                line = trial
        lines.append(line)
        for text in lines[:7]:
            draw.text((12, y), text, font=fonts["small"], fill=theme["warn"])
            y += 16
    elif not windows:
        draw.text((12, y), "Waiting for first reading...",
                  font=fonts["label"], fill=theme["muted"])
    for w in windows:
        remaining = max(0.0, min(100.0, 100 - w["utilization"]))
        sev = "crit" if remaining <= 10 else \
            "low" if remaining <= 30 else "ok"
        fill = theme["crit"] if sev == "crit" else \
            theme["warn"] if sev == "low" else theme["accent"]
        track = theme[("crit" if sev == "crit" else
                       "warn" if sev == "low" else "accent") + "_track"]
        label = SHORT_LABELS.get(w.get("key"), str(w["label"])[:14])
        draw.text((12, y), label, font=fonts["label"], fill=theme["ink"])
        value = f"{remaining:.0f}% left"
        tw = draw.textlength(value, font=fonts["big"])
        draw.text((228 - tw, y - 1), value, font=fonts["big"],
                  fill=theme["ink"])
        draw.rounded_rectangle((12, y + 18, 228, y + 29), 5, fill=track)
        draw.rounded_rectangle(
            (12, y + 18, 12 + max(6, int(216 * remaining / 100)), y + 29),
            5, fill=fill)
        draw.text((12, y + 31), _reset_text(w.get("resets_at")),
                  font=fonts["small"], fill=theme["muted"])
        y += 48
    _draw_wifi_footer(draw, snapshot, theme, fonts)
    return img


def run(snapshot_fn, config):
    """Drive the HAT display forever. Degrades to a no-op without hardware."""
    try:
        panel = ST7789()
    except Exception as exc:  # missing libs, no SPI device, no HAT
        print(f"HAT display off ({exc.__class__.__name__}: {exc}); "
              "web dashboard still available.", file=sys.stderr)
        return
    fonts = _load_fonts()
    sprites = _load_sprites()
    setup_url = _local_url(config, "/setup")
    frame = 0
    while True:
        if frame % 60 == 0:  # refresh in case the IP changed / came up late
            setup_url = _local_url(config, "/setup") or setup_url
        try:
            snap = snapshot_fn()
            panel.show(render(snap, frame, fonts, sprites, setup_url))
            panel.set_brightness(_target_brightness(snap))
        except Exception as exc:
            print(f"HAT display error: {exc}", file=sys.stderr)
            time.sleep(10)
        frame += 1
        time.sleep(0.5)
