// Headroom Mini — Claude usage meters on a Waveshare ESP32-S3-Touch-LCD-2.
//
// v0 scope: join Wi-Fi (first boot: its own "Headroom-Setup" hotspot with a
// phone setup page, like the Pi version), then speak the same HTTP API as the
// Pi tracker — GET /api/status with the "Headroom" discovery marker and
// POST /api/push — so the existing desktop companion feeds it with no changes.
// Phase 2 (later): poll Anthropic's usage endpoint directly on-device.
//
// Board facts (cross-checked against community drivers for this exact board):
//   LCD  ST7789 240x320 IPS: SCLK=39 MOSI=38 MISO=40 DC=42 CS=45 BL=1 RST=soft
//   Touch CST816D (unused in v0): SDA=48 SCL=47 addr 0x15

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <math.h>
#include <ctype.h>
#include <time.h>

// ---------------------------------------------------------------- pins / lcd

#define LCD_SCLK 39
#define LCD_MOSI 38
#define LCD_MISO 40
#define LCD_DC   42
#define LCD_CS   45
#define LCD_BL    1
#define LCD_RST  -1   // no reset line; ST7789 soft reset

static Arduino_DataBus *bus =
    new Arduino_ESP32SPI(LCD_DC, LCD_CS, LCD_SCLK, LCD_MOSI, LCD_MISO);
// rotation 2 = portrait 240x320 flipped 180° (USB-C connector at the top)
static Arduino_GFX *gfx =
    new Arduino_ST7789(bus, LCD_RST, 2 /*rotation*/, true /*IPS*/, 240, 320);

// Claude night palette in RGB565 (macro provided by Arduino_GFX)
static const uint16_t C_BG    = RGB565(0x26, 0x26, 0x24);
static const uint16_t C_INK   = RGB565(0xF5, 0xF4, 0xEF);
static const uint16_t C_MUTED = RGB565(0x94, 0x90, 0x7E);
static const uint16_t C_ACC   = RGB565(0xD9, 0x77, 0x57);   // Claude orange
static const uint16_t C_ACC_T = RGB565(0x4A, 0x38, 0x2F);
static const uint16_t C_WARN  = RGB565(0xFA, 0xB2, 0x19);
static const uint16_t C_WARN_T= RGB565(0x46, 0x3B, 0x1A);
static const uint16_t C_CRIT  = RGB565(0xE0, 0x52, 0x52);
static const uint16_t C_CRIT_T= RGB565(0x4A, 0x27, 0x27);

// Sprocket, the mascot
static const uint16_t C_SPRK  = RGB565(0x5F, 0x83, 0xA1);   // body
static const uint16_t C_SPRK_D= RGB565(0x3F, 0x5F, 0x7A);   // shade
static const uint16_t C_OUT   = RGB565(0x1A, 0x18, 0x16);   // outline / features
static const uint16_t C_FACE  = RGB565(0xFA, 0xF7, 0xEF);   // face screen

// ------------------------------------------------------------------- state

struct Window {
  char key[24];
  char label[28];
  float utilization;      // % used, 0..100
  time_t resets_at;       // UTC epoch, 0 if unknown
};

static const int MAX_WINDOWS = 4;
static Window windows[MAX_WINDOWS];
static int nWindows = 0;
static char plan[16] = "";
static unsigned long lastPushMs = 0;   // millis() of last accepted push
static bool timeSynced = false;

static Preferences prefs;
static WebServer *server = nullptr;
static DNSServer dns;
static bool apMode = false;

// Same defaults as the Pi build
static const char *AP_SSID = "Headroom-Setup";
static const char *AP_PSK  = "headroom";
static const int   API_PORT = 8080;   // what the companion probes
static const char *FW_VERSION = "1.0.0";

// Phase 2 — self-contained: poll Anthropic's usage endpoint directly, using an
// OAuth login pasted once via /connect. Same contract the companion uses.
static const char *CLIENT_ID   = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
static const char *REFRESH_URL = "https://platform.claude.com/v1/oauth/token";
static const char *USAGE_URL   = "https://api.anthropic.com/api/oauth/usage";
static const char *OAUTH_BETA  = "oauth-2025-04-20";
static const char *UA          = "Headroom-Mini/1.0.0";
static const unsigned long POLL_INTERVAL_MS = 5UL * 60UL * 1000UL;

static String   accessTok, refreshTok;
static uint64_t tokenExpMs = 0;       // epoch ms, 0 = unknown
static bool     selfHosted = false;   // true once a login is stored
static char     pollStatus[48] = "";  // last on-device poll result (shown when no data)

// UI / input state (Phase 1.5)
static const int BL_CHANNEL = 0;      // LEDC channel for backlight PWM
static const int BOOT_BTN    = 0;     // BOOT button -> hold to factory reset
static const int BAT_ADC_PIN = 5;     // VBAT via 200K/100K divider (x3), ADC1_CH4
static int       batPct      = -1;    // -1 = no battery / hidden
static bool      batCharging = false;
static uint8_t   backlight   = 255;   // 0..255
static bool      showUsed    = false; // false = "% left", true = "% used"
static bool      screenOff   = false; // face-down / manual dim
static char      tzEnv[48]   = "EST5EDT,M3.2.0,M11.1.0";  // POSIX TZ, set via /settings
static bool      clock24     = false; // false = 12-hour (3:45 PM), true = 24-hour
static bool      nightDim    = true;  // ease the backlight down overnight
static const uint8_t NIGHT_LEVEL = 40;
static int       uiScreen    = 0;     // 0 = meters, 1 = focus, 2 = history, 3 = Sprocket
static const int UI_SCREENS  = 4;

// Usage history: a ring buffer of the headline utilization, one sample every
// SAMPLE_INTERVAL_MS, persisted to flash hourly so it survives reboots.
static const int HIST_LEN = 60;
static uint8_t   histBuf[HIST_LEN];
static int       histCount = 0;       // valid samples so far (<= HIST_LEN)
static int       histHead  = 0;       // ring write index
static const unsigned long SAMPLE_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 min

// 10pm-7am local (once NTP has synced). Shared by night-dim and Sprocket.
static bool nightNow() {
  time_t now = time(nullptr);
  if (!timeSynced || now < 100000) return false;
  struct tm t;
  localtime_r(&now, &t);
  return t.tm_hour >= 22 || t.tm_hour < 7;
}

// Effective backlight = 0 if screen is off, capped to NIGHT_LEVEL overnight,
// else the user's brightness. Keeps the daytime preference intact.
static void applyBacklight() {
  uint8_t eff = backlight;
  if (nightDim && nightNow() && eff > NIGHT_LEVEL) eff = NIGHT_LEVEL;
  ledcWrite(BL_CHANNEL, screenOff ? 0 : eff);
}

static void setBacklight(uint8_t v) {
  backlight = v;
  applyBacklight();
}

// ------------------------------------------------------------ small helpers

// Parse "YYYY-MM-DDTHH:MM:SS..." (assumed UTC) to epoch. Ignores the offset
// suffix — Anthropic reset times arrive as UTC (+00:00 / Z).
static time_t parseISO(const char *s) {
  int y, mo, d, h, mi, se;
  if (!s || sscanf(s, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &se) != 6)
    return 0;
  int yy = y - (mo <= 2);
  int era = (yy >= 0 ? yy : yy - 399) / 400;
  unsigned yoe = (unsigned)(yy - era * 400);
  unsigned doy = (153u * (unsigned)(mo + (mo > 2 ? -3 : 9)) + 2u) / 5u + (unsigned)d - 1u;
  unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
  long days = (long)era * 146097L + (long)doe - 719468L;
  return (time_t)days * 86400 + h * 3600 + mi * 60 + se;
}

static void fmtCountdown(time_t resets, char *out, size_t n) {
  time_t now = time(nullptr);
  if (!resets || !timeSynced || now < 100000) { out[0] = 0; return; }
  long mins = (long)((resets - now) / 60);
  if (mins <= 0) { snprintf(out, n, "resetting..."); return; }
  long dd = mins / 1440, hh = (mins % 1440) / 60, mm = mins % 60;
  if (dd > 0)      snprintf(out, n, "resets in %ldd %ldh", dd, hh);
  else if (hh > 0) snprintf(out, n, "resets in %ldh %ldm", hh, mm);
  else             snprintf(out, n, "resets in %ldm", mm);
}

// ------------------------------------------------------------------ drawing

static void drawCentered(const char *text, int y, uint8_t size, uint16_t color) {
  int16_t x1, y1; uint16_t w, h;
  gfx->setTextSize(size);
  gfx->getTextBounds(text, 0, 0, &x1, &y1, &w, &h);
  gfx->setCursor((240 - (int)w) / 2, y);
  gfx->setTextColor(color);
  gfx->print(text);
}

static void drawSplash(const char *line1, const char *line2) {
  gfx->fillScreen(C_BG);
  drawCentered("HEADROOM", 130, 3, C_ACC);
  if (line1) drawCentered(line1, 170, 1, C_INK);
  if (line2) drawCentered(line2, 190, 1, C_MUTED);
}

// Read VBAT (GPIO5, 200K/100K divider -> x3) and map to a rough Li-ion %.
static void readBattery() {
  uint32_t mv = 0;
  for (int i = 0; i < 8; i++) mv += analogReadMilliVolts(BAT_ADC_PIN);
  float v = (mv / 8) * 3.0f / 1000.0f;             // undo the divider
  if (v < 2.5f) { batPct = -1; batCharging = false; return; }  // no battery
  int pct;
  if      (v >= 4.15f) pct = 100;
  else if (v >= 3.72f) pct = 50 + (int)((v - 3.72f) / (4.15f - 3.72f) * 50);
  else if (v >= 3.49f) pct = 10 + (int)((v - 3.49f) / (3.72f - 3.49f) * 40);
  else if (v >= 3.30f) pct =  5 + (int)((v - 3.30f) / (3.49f - 3.30f) *  5);
  else                 pct = 0;
  batPct = pct > 100 ? 100 : (pct < 0 ? 0 : pct);
  batCharging = v >= 4.25f;                          // held above full = on USB
}

// Small battery glyph at (x,y); nothing drawn when no battery is present.
static void drawBattery(int x, int y) {
  if (batPct < 0) return;
  const int w = 24, h = 12;
  uint16_t c = batPct <= 10 ? C_CRIT : batPct <= 30 ? C_WARN : C_ACC;
  gfx->drawRect(x, y, w, h, C_MUTED);
  gfx->fillRect(x + w, y + 3, 2, h - 6, C_MUTED);    // terminal nub
  int fw = (w - 4) * batPct / 100;
  if (fw > 0) gfx->fillRect(x + 2, y + 2, fw, h - 4, c);
  if (batCharging) {                                 // '+' = charging / on USB
    gfx->setTextSize(1);
    gfx->setTextColor(C_ACC);
    gfx->setCursor(x - 8, y + 3);
    gfx->print("+");
  }
}

static void drawMeters() {
  gfx->fillScreen(C_BG);

  // header: clock (hidden until NTP syncs), with the plan as a caption under it
  char buf[40];
  time_t now = time(nullptr);
  if (timeSynced && now > 100000) {
    struct tm tmnow;
    localtime_r(&now, &tmnow);
    strftime(buf, sizeof(buf), clock24 ? "%H:%M" : "%I:%M %p", &tmnow);
    const char *clk = buf;
    if (!clock24 && buf[0] == '0') clk = buf + 1;   // "03:45 PM" -> "3:45 PM"
    gfx->setTextSize(4);
    gfx->setTextColor(C_INK);
    gfx->setCursor(10, 10);
    gfx->print(clk);
  } else {
    gfx->setTextSize(2);
    gfx->setTextColor(C_MUTED);
    gfx->setCursor(10, 16);
    gfx->print("--:--");
  }
  if (plan[0]) {
    snprintf(buf, sizeof(buf), "%s plan", plan);
    gfx->setTextSize(1);
    gfx->setTextColor(C_MUTED);
    gfx->setCursor(12, 46);
    gfx->print(buf);
  }

  if (nWindows == 0) {
    if (selfHosted) {
      bool err = pollStatus[0] && strcmp(pollStatus, "not paired yet") != 0;
      drawCentered(pollStatus[0] ? pollStatus : "Fetching your usage...",
                   150, 1, err ? C_WARN : C_MUTED);
      if (strstr(pollStatus, "re-pair"))
        drawCentered("on your computer: companion --pair", 172, 1, C_ACC);
    } else if (lastPushMs == 0) {
      drawCentered("Set me up - open", 140, 1, C_MUTED);
      snprintf(buf, sizeof(buf), "http://%s:%d",
               WiFi.localIP().toString().c_str(), API_PORT);
      drawCentered(buf, 162, 1, C_ACC);
      drawCentered("(or run the companion on your PC)", 186, 1, C_MUTED);
    } else {
      drawCentered("No usage windows", 150, 1, C_MUTED);
    }
  }

  // meters: label / big % left / bar / countdown
  int y = 60;
  for (int i = 0; i < nWindows && i < 3; i++) {
    Window &w = windows[i];
    float left = 100.0f - w.utilization;
    if (left < 0) left = 0; if (left > 100) left = 100;
    uint16_t fill  = left <= 10 ? C_CRIT  : left <= 30 ? C_WARN  : C_ACC;
    uint16_t track = left <= 10 ? C_CRIT_T: left <= 30 ? C_WARN_T: C_ACC_T;

    gfx->setTextSize(2);
    gfx->setTextColor(C_INK);
    gfx->setCursor(12, y);
    gfx->print(w.label);

    if (showUsed) snprintf(buf, sizeof(buf), "%d%% used", (int)(w.utilization + 0.5f));
    else          snprintf(buf, sizeof(buf), "%d%% left", (int)(left + 0.5f));
    int16_t x1, y1; uint16_t tw, th;
    gfx->setTextSize(2);
    gfx->getTextBounds(buf, 0, 0, &x1, &y1, &tw, &th);
    gfx->setCursor(228 - (int)tw, y + 22);
    gfx->print(buf);

    int barY = y + 44;
    gfx->fillRoundRect(12, barY, 216, 14, 7, track);
    int wpx = (int)(216.0f * left / 100.0f);
    if (wpx < 8) wpx = 8;
    gfx->fillRoundRect(12, barY, wpx, 14, 7, fill);

    fmtCountdown(w.resets_at, buf, sizeof(buf));
    gfx->setTextSize(1);
    gfx->setTextColor(C_MUTED);
    gfx->setCursor(12, barY + 20);
    gfx->print(buf);

    y += 84;
  }

  // footer: ip + staleness
  gfx->setTextSize(1);
  gfx->setTextColor(C_MUTED);
  gfx->setCursor(10, 306);
  if (lastPushMs && millis() - lastPushMs > 10UL * 60UL * 1000UL) {
    gfx->setTextColor(C_WARN);
    gfx->print("stale - no update >10m");
  } else {
    gfx->print(WiFi.localIP().toString());
    gfx->print("  headroom.local");
  }
  drawBattery(212, 305);
}

// Focus screen: the most-constrained window, big — glanceable across a room.
static void drawFocus() {
  gfx->fillScreen(C_BG);
  if (nWindows == 0) { drawMeters(); return; }   // nothing to focus yet
  drawBattery(206, 8);

  int idx = 0;
  for (int i = 1; i < nWindows; i++)
    if (windows[i].utilization > windows[idx].utilization) idx = i;  // least left
  Window &w = windows[idx];
  float left = 100.0f - w.utilization;
  if (left < 0) left = 0; if (left > 100) left = 100;
  uint16_t fill = left <= 10 ? C_CRIT : left <= 30 ? C_WARN : C_ACC;

  drawCentered(w.label, 44, 3, C_INK);

  char buf[24];
  int val = showUsed ? (int)(w.utilization + 0.5f) : (int)(left + 0.5f);
  snprintf(buf, sizeof(buf), "%d%%", val);
  drawCentered(buf, 108, 8, fill);
  drawCentered(showUsed ? "used" : "left", 196, 2, C_MUTED);

  // big progress bar
  int barY = 232;
  gfx->fillRoundRect(20, barY, 200, 18, 9, C_ACC_T);
  int wpx = (int)(200.0f * left / 100.0f);
  if (wpx < 10) wpx = 10;
  gfx->fillRoundRect(20, barY, wpx, 18, 9, fill);

  fmtCountdown(w.resets_at, buf, sizeof(buf));
  drawCentered(buf, 272, 2, C_MUTED);
}

// Headline metric to trend: the session window if present, else the fullest.
static int headlineUtil() {
  int best = -1;
  for (int i = 0; i < nWindows; i++) {
    if (!strcmp(windows[i].key, "five_hour"))
      return (int)(windows[i].utilization + 0.5f);
    if ((int)windows[i].utilization > best) best = (int)windows[i].utilization;
  }
  return best;   // -1 if no data yet
}

static void sampleHistory() {
  int u = headlineUtil();
  if (u < 0) return;
  histBuf[histHead] = (uint8_t)(u < 0 ? 0 : (u > 100 ? 100 : u));
  histHead = (histHead + 1) % HIST_LEN;
  if (histCount < HIST_LEN) histCount++;
}

static void saveHistory() {
  prefs.begin("headroom", false);
  prefs.putBytes("hist", histBuf, HIST_LEN);
  prefs.putInt("histc", histCount);
  prefs.putInt("histh", histHead);
  prefs.end();
}

static void loadHistory() {
  prefs.begin("headroom", true);
  size_t n = prefs.getBytes("hist", histBuf, HIST_LEN);
  if (n == HIST_LEN) {
    histCount = prefs.getInt("histc", 0);
    histHead  = prefs.getInt("histh", 0);
  } else {
    memset(histBuf, 0, HIST_LEN);
    histCount = histHead = 0;
  }
  prefs.end();
}

// Bar-graph of the session usage over the last ~10 hours.
static void drawHistory() {
  gfx->fillScreen(C_BG);
  drawCentered("History", 34, 3, C_INK);
  drawCentered("session usage over time", 74, 1, C_MUTED);
  drawBattery(206, 8);
  if (histCount == 0) {
    drawCentered("collecting...", 150, 1, C_MUTED);
    return;
  }
  const int gx = 16, gy = 104, gw = 208, gh = 150;
  gfx->drawFastHLine(gx, gy + gh, gw, C_MUTED);          // baseline (0%)
  gfx->drawFastHLine(gx, gy, gw, C_ACC_T);               // 100% guide
  float bw = (float)gw / HIST_LEN;
  int cur = 0, peak = 0;
  for (int i = 0; i < histCount; i++) {
    int idx = (histHead - histCount + i + 2 * HIST_LEN) % HIST_LEN;
    int v = histBuf[idx];
    if (i == histCount - 1) cur = v;
    if (v > peak) peak = v;
    int bh = gh * v / 100;
    int x = gx + (int)((HIST_LEN - histCount + i) * bw);
    int left = 100 - v;
    uint16_t c = left <= 10 ? C_CRIT : left <= 30 ? C_WARN : C_ACC;
    if (bh > 0) gfx->fillRect(x, gy + gh - bh, (int)bw + 1, bh, c);
  }
  char buf[32];
  snprintf(buf, sizeof(buf), "now %d%%   peak %d%%", cur, peak);
  drawCentered(buf, gy + gh + 16, 2, C_INK);
}

// Sprocket, the mascot. Reacts to remaining headroom (and the time of day).
static void drawMascot() {
  gfx->fillScreen(C_BG);
  static const char *const body[11] = {
      "...K...K...",  "...B...B...",  "..KKKKKKK..",
      ".KBBBBBBBK.",  ".KWWWWWWWK.",  ".KWWWWWWWK.",
      ".KWWWWWWWK.",  ".KWWWWWWWK.",  ".KBSBBBSBK.",
      ".KBBBBBBBK.",  "..KK...KK.."};
  const int S = 18, ox = (240 - 11 * S) / 2, oy = 44;
  for (int y = 0; y < 11; y++)
    for (int x = 0; x < 11; x++) {
      uint16_t c;
      switch (body[y][x]) {
        case 'K': c = C_OUT;    break;
        case 'W': c = C_FACE;   break;
        case 'B': c = C_SPRK;   break;
        case 'S': c = C_SPRK_D; break;
        default:  continue;
      }
      gfx->fillRect(ox + x * S, oy + y * S, S, S, c);
    }

  // mood from the most-constrained window + time of day
  int idx = -1;
  for (int i = 0; i < nWindows; i++)
    if (idx < 0 || windows[i].utilization > windows[idx].utilization) idx = i;
  int u = idx < 0 ? -1 : (int)(windows[idx].utilization + 0.5f);
  int left = u < 0 ? -1 : 100 - u;
  bool night = nightNow();
  int mood = left < 0 ? 4 : night ? 3 : left <= 10 ? 2 : left <= 30 ? 1 : 0;
  uint16_t mc = mood == 2 ? C_CRIT : mood == 1 ? C_WARN
              : mood == 0 ? C_ACC : C_MUTED;

  gfx->fillRect(ox + 3 * S, oy + S, S, S, mc);        // antenna balls glow w/ mood
  gfx->fillRect(ox + 7 * S, oy + S, S, S, mc);

  int ey = oy + 5 * S, lx = ox + 3 * S, rx = ox + 7 * S;   // eyes
  if (mood == 3) {                                    // asleep - closed
    gfx->fillRect(lx, ey + S / 2, S, S / 4, C_OUT);
    gfx->fillRect(rx, ey + S / 2, S, S / 4, C_OUT);
  } else if (mood == 2) {                             // tapped out - small
    gfx->fillRect(lx + S / 4, ey + S / 4, S / 2, S / 2, C_OUT);
    gfx->fillRect(rx + S / 4, ey + S / 4, S / 2, S / 2, C_OUT);
  } else {                                            // open
    gfx->fillRect(lx, ey, S, S, C_OUT);
    gfx->fillRect(rx, ey, S, S, C_OUT);
  }

  int my = oy + 7 * S, mx = ox + 4 * S;               // mouth
  if (mood == 0)      gfx->fillRect(mx, my + S / 3, 3 * S, S / 2, C_OUT);   // smile
  else if (mood == 1) gfx->fillRect(mx + S, my + S / 4, S, S / 2, C_OUT);   // worried o
  else if (mood == 3) gfx->fillRect(mx + S, my + S / 3, S, S / 3, C_OUT);   // sleepy
  else                gfx->fillRect(mx, my + S / 2, 3 * S, S / 5, C_OUT);   // flat

  if (mood == 3) drawCentered("z  z  z", oy - 4, 2, C_MUTED);

  const char *word = mood == 0 ? "plenty of headroom"
                   : mood == 1 ? "getting low"
                   : mood == 2 ? "tapped out"
                   : mood == 3 ? "good night" : "waiting for usage";
  int cy = oy + 11 * S + 14;
  drawCentered(word, cy, 2, mc);
  if (u >= 0) {
    char buf[40];
    int shown = showUsed ? u : left;
    snprintf(buf, sizeof(buf), "%s %d%% %s", windows[idx].label, shown,
             showUsed ? "used" : "left");
    drawCentered(buf, cy + 26, 1, C_MUTED);
    fmtCountdown(windows[idx].resets_at, buf, sizeof(buf));
    if (buf[0]) drawCentered(buf, cy + 42, 1, C_MUTED);
  }
}

// Draw whichever screen is active (data updates / ticks call this).
static void drawScreen() {
  if (uiScreen == 1)      drawFocus();
  else if (uiScreen == 2) drawHistory();
  else if (uiScreen == 3) drawMascot();
  else                    drawMeters();
}

// ------------------------------------------------------------ phone alerts
// POST to ntfy (and/or Pushover) when a window crosses a threshold. The board
// has no speaker, but a phone push reaches you anywhere. Edge-triggered with a
// recovery notice, so you get one "low" and one "recovered" per window.

static String ntfyTopic, poToken, poUser;
static int    alertPct = 90;              // notify at/above this % used
struct AlertState { char key[24]; bool over; };
static AlertState alertStates[MAX_WINDOWS] = {};

static bool alertsConfigured() {
  return ntfyTopic.length() > 0 || (poToken.length() > 0 && poUser.length() > 0);
}

static String urlEncode(const String &s) {
  String o;
  char b[4];
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.' || c == '~')
      o += c;
    else if (c == ' ') o += "%20";
    else { snprintf(b, sizeof(b), "%%%02X", (unsigned char)c); o += b; }
  }
  return o;
}

static void sendNtfy(const char *title, const char *body) {
  if (ntfyTopic.length() == 0) return;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient h;
  if (!h.begin(client, "https://ntfy.sh/" + ntfyTopic)) return;
  h.addHeader("Title", title);
  h.addHeader("Content-Type", "text/plain");
  h.POST(String(body));
  h.end();
}

static void sendPushover(const char *title, const char *body) {
  if (poToken.length() == 0 || poUser.length() == 0) return;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient h;
  if (!h.begin(client, "https://api.pushover.net/1/messages.json")) return;
  h.addHeader("Content-Type", "application/x-www-form-urlencoded");
  String form = "token=" + urlEncode(poToken) + "&user=" + urlEncode(poUser) +
                "&title=" + urlEncode(title) + "&message=" + urlEncode(body);
  h.POST(form);
  h.end();
}

static void sendAlert(const char *title, const char *body) {
  if (WiFi.status() != WL_CONNECTED) return;
  sendNtfy(title, body);
  sendPushover(title, body);
}

// Edge-triggered per window: fire once on crossing up, once on recovery.
static void checkAlerts() {
  if (!alertsConfigured()) return;
  for (int i = 0; i < nWindows; i++) {
    Window &w = windows[i];
    int used = (int)(w.utilization + 0.5f);
    AlertState *st = nullptr;
    for (AlertState &a : alertStates)
      if (a.key[0] && !strcmp(a.key, w.key)) { st = &a; break; }
    if (!st)
      for (AlertState &a : alertStates)
        if (!a.key[0]) { strlcpy(a.key, w.key, sizeof(a.key)); a.over = false; st = &a; break; }
    if (!st) continue;
    char body[80];
    if (used >= alertPct && !st->over) {
      st->over = true;
      snprintf(body, sizeof(body), "%s at %d%% used", w.label, used);
      sendAlert("Headroom", body);
    } else if (used < alertPct - 10 && st->over) {
      st->over = false;
      snprintf(body, sizeof(body), "%s recovered (%d%% used)", w.label, used);
      sendAlert("Headroom", body);
    }
  }
}

static void saveAlerts() {
  prefs.begin("headroom", false);
  prefs.putString("ntfy", ntfyTopic);
  prefs.putString("potok", poToken);
  prefs.putString("pouser", poUser);
  prefs.putInt("alpct", alertPct);
  prefs.end();
}

// ------------------------------------------------------------------ web api

static void sendJson(int code, const String &body) {
  server->send(code, "application/json", body);
}

static void handleStatus() {
  JsonDocument doc;
  doc["app"] = "Headroom";        // discovery marker the companion looks for
  doc["mini"] = true;
  doc["plan"] = plan[0] ? plan : (const char *)nullptr;
  JsonArray arr = doc["windows"].to<JsonArray>();
  for (int i = 0; i < nWindows; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["key"] = windows[i].key;
    o["label"] = windows[i].label;
    o["utilization"] = windows[i].utilization;
  }
  doc["server_time"] = (long)time(nullptr);
  String out;
  serializeJson(doc, out);
  sendJson(200, out);
}

static void handlePush() {
  if (server->hasArg("plain") == false) {
    sendJson(400, "{\"ok\":false,\"error\":\"no body\"}");
    return;
  }
  const String &body = server->arg("plain");
  if (body.length() > 8192) {
    sendJson(413, "{\"ok\":false,\"error\":\"too large\"}");
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, body) != DeserializationError::Ok ||
      !doc["windows"].is<JsonArray>()) {
    sendJson(400, "{\"ok\":false,\"error\":\"bad json\"}");
    return;
  }
  nWindows = 0;
  for (JsonObject w : doc["windows"].as<JsonArray>()) {
    if (nWindows >= MAX_WINDOWS) break;
    if (!w["utilization"].is<float>() && !w["utilization"].is<int>()) continue;
    Window &dst = windows[nWindows++];
    strlcpy(dst.key, w["key"] | "", sizeof(dst.key));
    strlcpy(dst.label, w["label"] | "Usage", sizeof(dst.label));
    // compact long labels for a 2" screen
    if (strcmp(dst.key, "five_hour") == 0) strlcpy(dst.label, "Session", sizeof(dst.label));
    else if (strcmp(dst.key, "seven_day") == 0) strlcpy(dst.label, "Weekly", sizeof(dst.label));
    else if (strcmp(dst.key, "seven_day_opus") == 0) strlcpy(dst.label, "Opus", sizeof(dst.label));
    float u = w["utilization"].as<float>();
    dst.utilization = u < 0 ? 0 : (u > 100 ? 100 : u);
    dst.resets_at = parseISO(w["resets_at"] | (const char *)nullptr);
  }
  strlcpy(plan, doc["plan"] | "", sizeof(plan));
  lastPushMs = millis();
  sendJson(200, "{\"ok\":true}");
  checkAlerts();
  drawScreen();
}

// Wi-Fi provisioning portal (AP mode). The page fetches /scan for a tappable
// list of nearby networks so nothing has to be typed by hand (manual entry
// stays as a fallback).
static const char PORTAL_HTML[] PROGMEM = R"HTML(<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Headroom Wi-Fi setup</title>
<style>body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:24px 18px;margin:0}
.card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;padding:16px;max-width:420px;margin:0 auto}
h2{margin:.2rem 0 .6rem}p{margin:.4rem 0}
input{width:100%;padding:12px;font-size:1rem;border-radius:10px;border:1px solid rgba(61,57,41,.25);margin:6px 0 12px;box-sizing:border-box}
button{display:block;width:100%;background:#d97757;color:#fff;font-weight:600;font-size:1.05rem;padding:14px;border-radius:10px;border:none}
#list{margin:6px 0 12px}
.net{background:#fff;color:#3d3929;text-align:left;font-weight:500;font-size:1rem;padding:12px 14px;margin:6px 0;border:1px solid rgba(61,57,41,.18);border-radius:10px;display:flex;justify-content:space-between;align-items:center}
.net.sel{border-color:#d97757;background:#fbeee8}
.net .bars{color:#94907e;font-size:.85rem;margin-left:10px}
.row{display:flex;gap:8px;align-items:center;margin:6px 0}
.row button{width:auto;padding:8px 12px;font-size:.9rem;background:#e9e6dc;color:#3d3929}
.muted{color:#94907e;font-size:.9rem}</style>
</head><body><div class="card">
<h2>Connect Headroom to Wi-Fi</h2>
<div class="row"><strong>Pick your network</strong>
<button type="button" id="rescan">Rescan</button></div>
<div id="list" class="muted">Scanning&hellip;</div>
<form method="POST" action="/wifi">
<input name="ssid" id="ssid" placeholder="Network name (SSID)" autocapitalize="off" autocorrect="off">
<input name="password" id="pw" type="password" placeholder="Wi-Fi password">
<button type="submit">Connect</button></form>
<p class="muted">Not listed? Type the name above.</p>
</div>
<script>
function bars(r){var n=r>=-55?4:r>=-65?3:r>=-75?2:1;return '•'.repeat(n)+'·'.repeat(4-n);}
function pick(el,ssid){document.querySelectorAll('.net').forEach(function(x){x.classList.remove('sel');});
el.classList.add('sel');document.getElementById('ssid').value=ssid;document.getElementById('pw').focus();}
function load(){var L=document.getElementById('list');L.textContent='Scanning…';L.className='muted';
fetch('/scan').then(function(r){return r.json();}).then(function(nets){
if(!nets.length){L.textContent='No networks found. Type the name below.';return;}
L.innerHTML='';L.className='';
nets.forEach(function(n){var b=document.createElement('button');b.type='button';b.className='net';
b.innerHTML='<span>'+(n.lock?'🔒 ':'')+n.ssid.replace(/</g,'&lt;')+'</span><span class="bars">'+bars(n.rssi)+'</span>';
b.onclick=function(){pick(b,n.ssid);};L.appendChild(b);});
}).catch(function(){L.textContent='Scan failed — type your network below.';});}
document.getElementById('rescan').onclick=load;load();
</script>
</body></html>)HTML";

static void handlePortal() { server->send(200, "text/html", PORTAL_HTML); }

// Return nearby networks as JSON: [{"ssid","rssi","lock"}...], strongest first,
// deduped by name. Runs in AP+STA mode so the phone stays connected mid-scan.
static void handleScan() {
  int n = WiFi.scanNetworks(false /*async*/, false /*hidden*/);
  JsonDocument doc;
  JsonArray arr = doc.to<JsonArray>();
  for (int i = 0; i < n && arr.size() < 24; i++) {
    String ssid = WiFi.SSID(i);
    if (ssid.length() == 0) continue;
    bool dup = false;
    for (JsonObject o : arr)
      if (ssid == (const char *)(o["ssid"] | "")) { dup = true; break; }
    if (dup) continue;
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = ssid;
    o["rssi"] = WiFi.RSSI(i);
    o["lock"] = WiFi.encryptionType(i) != WIFI_AUTH_OPEN;
  }
  WiFi.scanDelete();
  String out;
  serializeJson(doc, out);
  sendJson(200, out);
}

static void handleWifiSave() {
  String ssid = server->arg("ssid");
  String pass = server->arg("password");
  if (ssid.length() == 0 || ssid.length() > 64) {
    server->send(200, "text/html",
                 "<p>Pick a network name first. <a href=/>back</a></p>");
    return;
  }
  prefs.begin("headroom", false);
  prefs.putString("ssid", ssid);
  prefs.putString("psk", pass);
  prefs.end();
  server->send(200, "text/html",
               "<h2>Saved — rebooting.</h2><p>Headroom will join your network."
               " Watch its screen for the address.</p>");
  delay(1200);
  ESP.restart();
}

// ---------------------------------------------------- Improv Wi-Fi (serial)
// Lets the browser flasher (ESP Web Tools) provision Wi-Fi over the same USB
// cable used to flash — the browser asks for your network right after Install
// and sends it down the wire. No hotspot, no typing an address.
// Protocol: https://www.improv-wifi.com/serial/

namespace improv {
enum Type : uint8_t { T_CURRENT_STATE = 0x01, T_ERROR = 0x02,
                      T_RPC = 0x03, T_RPC_RESPONSE = 0x04 };
enum State : uint8_t { S_AUTHORIZED = 0x02, S_PROVISIONING = 0x03,
                       S_PROVISIONED = 0x04 };
enum Err : uint8_t { E_NONE = 0x00, E_INVALID_RPC = 0x01, E_UNKNOWN_RPC = 0x02,
                     E_CANNOT_CONNECT = 0x03, E_UNKNOWN = 0xFF };
enum Cmd : uint8_t { C_WIFI_SETTINGS = 0x01, C_REQUEST_STATE = 0x02,
                     C_REQUEST_INFO = 0x03, C_REQUEST_SCAN = 0x04 };
static const char HEADER[6] = {'I', 'M', 'P', 'R', 'O', 'V'};
}  // namespace improv

static uint8_t improvRx[288];
static size_t improvPos = 0;

// Frame and emit one Improv packet: IMPROV + ver + type + len + data + cksum.
static void improvSend(uint8_t type, const uint8_t *data, uint8_t len) {
  uint8_t pkt[288];
  size_t n = 0;
  memcpy(pkt, improv::HEADER, 6); n = 6;
  pkt[n++] = 1;             // protocol version
  pkt[n++] = type;
  pkt[n++] = len;
  for (uint8_t i = 0; i < len; i++) pkt[n++] = data[i];
  uint32_t sum = 0;
  for (size_t i = 0; i < n; i++) sum += pkt[i];
  pkt[n++] = (uint8_t)(sum & 0xFF);
  Serial.write(pkt, n);
  Serial.write('\n');
}

static void improvSendState(uint8_t s) { improvSend(improv::T_CURRENT_STATE, &s, 1); }
static void improvSendError(uint8_t e) { improvSend(improv::T_ERROR, &e, 1); }

// RPC response: [cmd][blobLen][ (len-prefixed string)* ].
static void improvSendResult(uint8_t cmd, const char *const *strs, uint8_t nstrs) {
  uint8_t d[256];
  size_t n = 0;
  d[n++] = cmd;
  size_t lenAt = n++;       // filled in once the strings are laid down
  size_t start = n;
  for (uint8_t i = 0; i < nstrs; i++) {
    uint8_t sl = (uint8_t)strlen(strs[i]);
    if (n + 1 + sl > sizeof(d)) break;
    d[n++] = sl;
    memcpy(d + n, strs[i], sl); n += sl;
  }
  d[lenAt] = (uint8_t)(n - start);
  improvSend(improv::T_RPC_RESPONSE, d, (uint8_t)n);
}

// The device URL the browser should open once we're online.
static void improvSendURL() {
  char url[48];
  snprintf(url, sizeof(url), "http://%s", WiFi.localIP().toString().c_str());
  const char *urls[1] = {url};
  improvSendResult(improv::C_WIFI_SETTINGS, urls, 1);
}

// One RPC result per network, then an empty result to mark the end. Lets the
// browser show a dropdown so only the password has to be typed.
static void improvSendScan() {
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n; i++) {
    char ssid[33];
    strlcpy(ssid, WiFi.SSID(i).c_str(), sizeof(ssid));
    if (ssid[0] == 0) continue;
    char rssi[8];
    snprintf(rssi, sizeof(rssi), "%d", WiFi.RSSI(i));
    const char *row[3] = {ssid, rssi,
                          WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "NO" : "YES"};
    improvSendResult(improv::C_REQUEST_SCAN, row, 3);
  }
  improvSendResult(improv::C_REQUEST_SCAN, nullptr, 0);
  WiFi.scanDelete();
}

static void improvConnect(const char *ssid, const char *pass) {
  improvSendState(improv::S_PROVISIONING);
  gfx->fillScreen(C_BG);
  drawCentered("Connecting to Wi-Fi", 120, 2, C_INK);
  drawCentered(ssid, 158, 1, C_MUTED);

  WiFi.mode(WIFI_STA);
  WiFi.setHostname("headroom");
  WiFi.begin(ssid, pass);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) delay(200);

  if (WiFi.status() != WL_CONNECTED) {
    improvSendError(improv::E_CANNOT_CONNECT);
    drawCentered("Couldn't connect - check password", 196, 1, C_CRIT);
    return;
  }
  prefs.begin("headroom", false);
  prefs.putString("ssid", ssid);
  prefs.putString("psk", pass);
  prefs.end();
  improvSendState(improv::S_PROVISIONED);
  improvSendURL();
  Serial.flush();
  delay(600);
  ESP.restart();                 // reboot into the normal STA + API flow
}

static void improvDispatch() {
  if (improvRx[7] != improv::T_RPC) return;   // we only handle commands
  uint8_t cmd = improvRx[9];
  uint8_t blob = improvRx[10];
  const uint8_t *d = &improvRx[11];
  switch (cmd) {
    case improv::C_REQUEST_STATE: {
      bool online = WiFi.status() == WL_CONNECTED;
      improvSendState(online ? improv::S_PROVISIONED : improv::S_AUTHORIZED);
      if (online) improvSendURL();
      break;
    }
    case improv::C_REQUEST_INFO: {
      const char *info[4] = {"Headroom Mini", FW_VERSION, "ESP32-S3", "Headroom"};
      improvSendResult(improv::C_REQUEST_INFO, info, 4);
      break;
    }
    case improv::C_REQUEST_SCAN:
      improvSendScan();
      break;
    case improv::C_WIFI_SETTINGS: {
      if (blob < 2) { improvSendError(improv::E_INVALID_RPC); break; }
      uint8_t sl = d[0];
      if (1 + sl + 1 > blob || sl > 64) { improvSendError(improv::E_INVALID_RPC); break; }
      uint8_t pl = d[1 + sl];
      if (2 + sl + pl > blob || pl > 64) { improvSendError(improv::E_INVALID_RPC); break; }
      char ssid[65], pass[65];
      memcpy(ssid, d + 1, sl); ssid[sl] = 0;
      memcpy(pass, d + 2 + sl, pl); pass[pl] = 0;
      improvConnect(ssid, pass);
      break;
    }
    default:
      improvSendError(improv::E_UNKNOWN_RPC);
  }
}

// Feed one serial byte through the packet parser; dispatch on a valid frame.
static void improvByte(uint8_t b) {
  if (improvPos >= sizeof(improvRx)) improvPos = 0;
  if (improvPos < 6) {                            // resync on the IMPROV header
    if (b == (uint8_t)improv::HEADER[improvPos]) improvRx[improvPos++] = b;
    else { improvPos = 0; if (b == 'I') improvRx[improvPos++] = b; }
    return;
  }
  improvRx[improvPos++] = b;
  if (improvPos < 9) return;                      // need version, type, length
  size_t total = 9 + (size_t)improvRx[8] + 1;     // +data +checksum
  if (improvPos < total) return;
  uint32_t sum = 0;
  for (size_t i = 0; i < total - 1; i++) sum += improvRx[i];
  if ((uint8_t)(sum & 0xFF) == improvRx[total - 1]) improvDispatch();
  improvPos = 0;
}

static void improvPoll() {
  while (Serial.available()) improvByte((uint8_t)Serial.read());
}

// ------------------------------------------- Phase 2: on-device usage polling

static void loadCreds() {
  prefs.begin("headroom", true);
  accessTok  = prefs.getString("atok", "");
  refreshTok = prefs.getString("rtok", "");
  tokenExpMs = prefs.getULong64("exp", 0);
  strlcpy(plan, prefs.getString("plan", "").c_str(), sizeof(plan));
  showUsed   = prefs.getBool("used", false);
  ntfyTopic  = prefs.getString("ntfy", "");
  poToken    = prefs.getString("potok", "");
  poUser     = prefs.getString("pouser", "");
  alertPct   = prefs.getInt("alpct", 90);
  strlcpy(tzEnv, prefs.getString("tz", tzEnv).c_str(), sizeof(tzEnv));
  clock24    = prefs.getBool("clk24", false);
  nightDim   = prefs.getBool("ndim", true);
  prefs.end();
  selfHosted = accessTok.length() > 0;
}

static void applyTz() {
  setenv("TZ", tzEnv, 1);
  tzset();
}

static void saveCreds() {
  prefs.begin("headroom", false);
  prefs.putString("atok", accessTok);
  prefs.putString("rtok", refreshTok);
  prefs.putULong64("exp", tokenExpMs);
  prefs.putString("plan", plan);
  prefs.end();
}

// Adopt an oauth object (from the companion's --pair or a pasted login) and
// persist it. Accepts either the raw oauth dict or a {claudeAiOauth:{...}}.
static bool storeOauth(JsonObject root) {
  JsonObject o = root["claudeAiOauth"].is<JsonObject>()
                     ? root["claudeAiOauth"].as<JsonObject>()
                     : root;
  const char *at = o["accessToken"].as<const char *>();
  if (!at) at = o["access_token"].as<const char *>();
  if (!at) return false;
  accessTok = at;
  const char *rt = o["refreshToken"].as<const char *>();
  if (!rt) rt = o["refresh_token"].as<const char *>();
  refreshTok = rt ? rt : "";
  tokenExpMs = o["expiresAt"] | (uint64_t)0;
  if (!tokenExpMs) tokenExpMs = o["expires_at"] | (uint64_t)0;
  const char *sub = o["subscriptionType"].as<const char *>();
  strlcpy(plan, sub ? sub : "", sizeof(plan));
  selfHosted = true;
  saveCreds();
  return true;
}

// Exchange the rotating refresh token for a fresh access token, saving the new
// pair back (the refresh token rotates — losing it means re-pasting the login).
static bool refreshAccess() {
  if (refreshTok.length() == 0) return false;
  WiFiClientSecure client;
  client.setInsecure();   // no on-device CA store; TLS without cert pinning
  HTTPClient https;
  if (!https.begin(client, REFRESH_URL)) return false;
  https.addHeader("Content-Type", "application/json");
  https.addHeader("User-Agent", UA);
  JsonDocument body;
  body["grant_type"]   = "refresh_token";
  body["refresh_token"] = refreshTok;
  body["client_id"]    = CLIENT_ID;
  String out;
  serializeJson(body, out);
  int code = https.POST(out);
  if (code != 200) { https.end(); return false; }
  JsonDocument doc;
  DeserializationError e = deserializeJson(doc, https.getString());
  https.end();
  if (e) return false;
  const char *at = doc["access_token"].as<const char *>();
  if (!at) return false;
  accessTok = at;
  const char *rt = doc["refresh_token"].as<const char *>();
  if (rt) refreshTok = rt;
  long ein = doc["expires_in"] | 0;
  time_t now = time(nullptr);
  tokenExpMs = (ein && now > 100000) ? (uint64_t)(now + ein) * 1000ULL : 0;
  saveCreds();
  return true;
}

// Map an Anthropic usage window key to a short label that fits a 2" screen.
static const char *shortLabel(const char *key) {
  if (!strcmp(key, "five_hour"))            return "Session";
  if (!strcmp(key, "seven_day"))            return "Weekly";
  if (!strcmp(key, "seven_day_opus"))       return "Opus";
  if (!strcmp(key, "seven_day_sonnet"))     return "Sonnet";
  if (!strcmp(key, "seven_day_oauth_apps")) return "Apps";
  if (!strcmp(key, "extra_usage"))          return "Extra";
  return "Usage";
}

// GET the usage endpoint, parse windows, redraw. Refreshes once on 401/403.
static bool fetchUsage(bool allowRefresh) {
  if (accessTok.length() == 0) {
    strlcpy(pollStatus, "not paired yet", sizeof(pollStatus));
    return false;
  }
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient https;
  if (!https.begin(client, USAGE_URL)) {
    strlcpy(pollStatus, "can't reach Anthropic", sizeof(pollStatus));
    return false;
  }
  https.addHeader("Authorization", "Bearer " + accessTok);
  https.addHeader("anthropic-beta", OAUTH_BETA);
  https.addHeader("Accept", "application/json");
  https.addHeader("User-Agent", UA);
  int code = https.GET();
  if ((code == 401 || code == 403) && allowRefresh) {
    https.end();
    if (refreshAccess()) return fetchUsage(false);
    strlcpy(pollStatus, "login expired - re-pair", sizeof(pollStatus));
    return false;
  }
  if (code != 200) {
    https.end();
    if (code == 401 || code == 403)
      strlcpy(pollStatus, "login expired - re-pair", sizeof(pollStatus));
    else if (code == 429)
      strlcpy(pollStatus, "rate limited - retrying", sizeof(pollStatus));
    else if (code < 0)
      strlcpy(pollStatus, "can't reach Anthropic", sizeof(pollStatus));
    else
      snprintf(pollStatus, sizeof(pollStatus), "Anthropic error %d", code);
    return false;
  }
  String payload = https.getString();
  https.end();

  JsonDocument doc;
  if (deserializeJson(doc, payload) || doc.as<JsonObject>().isNull()) {
    strlcpy(pollStatus, "bad response", sizeof(pollStatus));
    return false;
  }
  JsonObject root = doc.as<JsonObject>();

  // Fixed display order, mirroring the companion's ordering.
  static const char *const ORDER[] = {
      "five_hour", "seven_day", "seven_day_sonnet",
      "seven_day_opus", "seven_day_oauth_apps", "extra_usage"};
  nWindows = 0;
  for (const char *key : ORDER) {
    if (nWindows >= MAX_WINDOWS) break;
    JsonVariant v = root[key];
    if (!v.is<JsonObject>() || v["utilization"].isNull()) continue;
    Window &w = windows[nWindows++];
    strlcpy(w.key, key, sizeof(w.key));
    strlcpy(w.label, shortLabel(key), sizeof(w.label));
    float u = v["utilization"].as<float>();
    w.utilization = u < 0 ? 0 : (u > 100 ? 100 : u);
    const char *r = v["resets_at"].as<const char *>();
    if (!r) r = v["resetsAt"].as<const char *>();
    w.resets_at = parseISO(r);
  }
  if (nWindows == 0) {
    strlcpy(pollStatus, "no usage windows", sizeof(pollStatus));
    return false;
  }
  pollStatus[0] = 0;   // success -> clear any prior error
  lastPushMs = millis();
  checkAlerts();
  drawScreen();
  return true;
}

static void pollUsage() {
  if (selfHosted && WiFi.status() == WL_CONNECTED) fetchUsage(true);
}

// ---- /connect web page: paste a Claude login once, board polls on its own ---

static void handleConnectPage() {
  String s = F(
      "<!DOCTYPE html><html><head><meta charset=utf-8>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<title>Headroom - connect account</title><style>"
      "body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:22px 16px;margin:0}"
      ".card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;"
      "padding:16px;max-width:520px;margin:0 auto}h2{margin:.2rem 0 .6rem}"
      "textarea{width:100%;height:120px;font-family:monospace;font-size:.82rem;"
      "border-radius:10px;border:1px solid rgba(61,57,41,.25);padding:10px;box-sizing:border-box}"
      "button{background:#d97757;color:#fff;font-weight:600;font-size:1rem;padding:12px 18px;"
      "border:none;border-radius:10px;margin-top:10px}"
      ".warn{background:#fbeee8;border:1px solid rgba(217,119,87,.35);border-radius:10px;"
      "padding:10px 12px;font-size:.9rem;margin:10px 0}.ok{color:#2e7d32;font-weight:600}"
      "code{background:rgba(61,57,41,.07);padding:1px 5px;border-radius:5px}"
      "</style></head><body><div class=card><h2>Connect your Claude account</h2>");
  if (selfHosted)
    s += F("<p class=ok>&#10003; Connected - the board polls your usage on its own.</p>");
  s += F(
      "<div class=warn><b>Easier way:</b> run the companion once with "
      "<code>--pair</code> and it sends this board your login automatically - "
      "no copying. This manual page is only for when you can't run it.</div>"
      "<p>Paste the contents of your Claude Code login. The board reads your "
      "usage directly, so no companion app has to keep running.</p>"
      "<ul><li><b>macOS:</b> Keychain item <code>Claude Code-credentials</code></li>"
      "<li><b>Windows/Linux:</b> <code>~/.claude/.credentials.json</code></li></ul>"
      "<div class=warn><b>Use a separate Claude login for the board.</b> If you "
      "paste the same login your computer's Claude Code uses, the two keep "
      "logging each other out. Best is a spare account just for the display.</div>"
      "<form method=POST action=/connect>"
      "<textarea name=creds placeholder='{&quot;claudeAiOauth&quot;:{...}}'></textarea>"
      "<button type=submit>Connect</button></form>");
  if (selfHosted)
    s += F("<form method=POST action=/disconnect>"
           "<button style='background:#8a8577'>Disconnect</button></form>");
  s += F("</div></body></html>");
  server->send(200, "text/html", s);
}

static void handleConnectSave() {
  String raw = server->arg("creds");
  JsonDocument doc;
  if (raw.length() == 0 || deserializeJson(doc, raw) ||
      !storeOauth(doc.as<JsonObject>())) {
    server->send(200, "text/html",
                 "<p>Couldn't read a login from that. <a href=/connect>back</a></p>");
    return;
  }
  bool ok = fetchUsage(true);
  String s = F("<!DOCTYPE html><html><head><meta charset=utf-8>"
               "<meta name=viewport content='width=device-width,initial-scale=1'>"
               "</head><body style='font-family:system-ui;padding:24px'>");
  if (ok)
    s += F("<h2>Connected &#10003;</h2><p>The board is showing your live usage. "
           "You can close this - it runs on its own now.</p>");
  else
    s += F("<h2>Saved, but the first read failed</h2><p>Make sure you pasted a "
           "current, valid login. The board will keep retrying.</p>");
  s += F("<p><a href=/connect>back</a></p></body></html>");
  server->send(200, "text/html", s);
}

// Companion --pair posts the oauth token here so the user never handles it.
static void handlePair() {
  if (!server->hasArg("plain")) { sendJson(400, "{\"ok\":false}"); return; }
  JsonDocument doc;
  if (deserializeJson(doc, server->arg("plain")) ||
      !storeOauth(doc.as<JsonObject>())) {
    sendJson(400, "{\"ok\":false,\"error\":\"no login in body\"}");
    return;
  }
  bool live = fetchUsage(true);
  sendJson(200, live ? "{\"ok\":true,\"live\":true}" : "{\"ok\":true,\"live\":false}");
}

static void handleDisconnect() {
  accessTok = ""; refreshTok = ""; tokenExpMs = 0; selfHosted = false;
  plan[0] = 0;
  prefs.begin("headroom", false);
  prefs.remove("atok"); prefs.remove("rtok");
  prefs.remove("exp");  prefs.remove("plan");
  prefs.end();
  nWindows = 0;
  server->send(200, "text/html", "<p>Disconnected. <a href=/connect>back</a></p>");
}

// ---- /alerts: phone push when usage gets high (ntfy topic / Pushover keys) --

static void handleAlertsPage() {
  char pct[8];
  snprintf(pct, sizeof(pct), "%d", alertPct);
  String s = F(
      "<!DOCTYPE html><html><head><meta charset=utf-8>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<title>Headroom - phone alerts</title><style>"
      "body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:22px 16px;margin:0}"
      ".card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;"
      "padding:16px;max-width:520px;margin:0 auto}h2{margin:.2rem 0 .6rem}label{font-size:.9rem}"
      "input{width:100%;padding:11px;font-size:1rem;border-radius:10px;"
      "border:1px solid rgba(61,57,41,.25);margin:4px 0 12px;box-sizing:border-box}"
      "button{background:#d97757;color:#fff;font-weight:600;font-size:1rem;padding:12px 18px;"
      "border:none;border-radius:10px}code{background:rgba(61,57,41,.07);padding:1px 5px;border-radius:5px}"
      ".muted{color:#94907e;font-size:.85rem}</style></head><body><div class=card>"
      "<h2>Phone alerts</h2>"
      "<p>Get a push when a window gets high. Easiest is <b>ntfy</b>: install "
      "the free ntfy app, pick any topic name, and enter it below.</p>"
      "<form method=POST action=/alerts>"
      "<label>ntfy topic</label>"
      "<input name=ntfy value='");
  s += ntfyTopic;
  s += F("' placeholder='e.g. headroom-dave-9f3'>"
         "<label>Alert at what % used?</label>"
         "<input name=pct type=number min=50 max=100 value='");
  s += pct;
  s += F("'>"
         "<details><summary class=muted>Pushover instead (optional)</summary>"
         "<label>Pushover API token</label>"
         "<input name=potok placeholder='");
  s += poToken.length() ? F("(saved - leave blank to keep)") : F("");
  s += F("'><label>Pushover user key</label><input name=pouser placeholder='");
  s += poUser.length() ? F("(saved - leave blank to keep)") : F("");
  s += F("'></details>"
         "<button type=submit>Save</button></form>"
         "<form method=POST action=/alerts/test style='margin-top:10px'>"
         "<button style='background:#8a8577'>Send test alert</button></form>"
         "<p class=muted>Recovery notice fires when it drops ~10% below the "
         "threshold.</p></div></body></html>");
  server->send(200, "text/html", s);
}

static void handleAlertsSave() {
  ntfyTopic = server->arg("ntfy");
  ntfyTopic.trim();
  int p = server->arg("pct").toInt();
  if (p >= 50 && p <= 100) alertPct = p;
  String pt = server->arg("potok"); pt.trim();
  String pu = server->arg("pouser"); pu.trim();
  if (pt.length()) poToken = pt;      // blank keeps the saved value
  if (pu.length()) poUser = pu;
  saveAlerts();
  server->send(200, "text/html",
               "<p>Saved. <a href=/alerts>back</a></p>");
}

static void handleAlertsTest() {
  if (!alertsConfigured()) {
    server->send(200, "text/html",
                 "<p>Set a topic or keys first. <a href=/alerts>back</a></p>");
    return;
  }
  sendAlert("Headroom", "Test alert - notifications are working.");
  server->send(200, "text/html",
               "<p>Sent - check your phone. <a href=/alerts>back</a></p>");
}

// ---- /settings: friendly timezone picker (clock only; countdowns are TZ-free)

static const char *TZ_OPTIONS[][2] = {
    {"US Eastern",          "EST5EDT,M3.2.0,M11.1.0"},
    {"US Central",          "CST6CDT,M3.2.0,M11.1.0"},
    {"US Mountain",         "MST7MDT,M3.2.0,M11.1.0"},
    {"US Arizona (no DST)", "MST7"},
    {"US Pacific",          "PST8PDT,M3.2.0,M11.1.0"},
    {"US Alaska",           "AKST9AKDT,M3.2.0,M11.1.0"},
    {"US Hawaii",           "HST10"},
    {"UK / Ireland",        "GMT0BST,M3.5.0/1,M10.5.0"},
    {"Central Europe",      "CET-1CEST,M3.5.0,M10.5.0/3"},
    {"India",               "IST-5:30"},
    {"Japan",               "JST-9"},
    {"Sydney",              "AEST-10AEDT,M10.1.0,M4.1.0/3"},
    {"UTC",                 "UTC0"},
};
static const int N_TZ = sizeof(TZ_OPTIONS) / sizeof(TZ_OPTIONS[0]);

static void handleSettingsPage() {
  String s = F(
      "<!DOCTYPE html><html><head><meta charset=utf-8>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<title>Headroom - settings</title><style>"
      "body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:22px 16px;margin:0}"
      ".card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;"
      "padding:16px;max-width:520px;margin:0 auto}h2{margin:.2rem 0 .6rem}label{font-size:.9rem}"
      "select{width:100%;padding:11px;font-size:1rem;border-radius:10px;"
      "border:1px solid rgba(61,57,41,.25);margin:4px 0 12px;box-sizing:border-box;background:#fff}"
      "button{background:#d97757;color:#fff;font-weight:600;font-size:1rem;padding:12px 18px;"
      "border:none;border-radius:10px}.muted{color:#94907e;font-size:.85rem}</style>"
      "</head><body><div class=card><h2>Settings</h2>"
      "<form method=POST action=/settings><label>Time zone (for the clock)</label>"
      "<select name=tz>");
  for (int i = 0; i < N_TZ; i++) {
    s += "<option value='";
    s += TZ_OPTIONS[i][1];
    s += "'";
    if (!strcmp(tzEnv, TZ_OPTIONS[i][1])) s += " selected";
    s += ">";
    s += TZ_OPTIONS[i][0];
    s += "</option>";
  }
  s += F("</select><label>Clock format</label><select name=clock>"
         "<option value=12");
  if (!clock24) s += " selected";
  s += F(">12-hour (3:45 PM)</option><option value=24");
  if (clock24) s += " selected";
  s += F(">24-hour (15:45)</option></select>"
         "<label>Overnight dimming</label><select name=ndim>"
         "<option value=on");
  if (nightDim) s += " selected";
  s += F(">On (dim 10pm-7am)</option><option value=off");
  if (!nightDim) s += " selected";
  s += F(">Off</option></select>"
         "<button type=submit>Save</button></form>"
         "<p class=muted>Reset countdowns are timezone-independent.</p></div></body></html>");
  server->send(200, "text/html", s);
}

static void handleSettingsSave() {
  prefs.begin("headroom", false);
  String tz = server->arg("tz");
  for (int i = 0; i < N_TZ; i++)
    if (tz == TZ_OPTIONS[i][1]) {              // only accept a listed value
      strlcpy(tzEnv, tz.c_str(), sizeof(tzEnv));
      prefs.putString("tz", tzEnv);
      applyTz();
      break;
    }
  if (server->hasArg("clock")) {
    clock24 = (server->arg("clock") == "24");
    prefs.putBool("clk24", clock24);
  }
  if (server->hasArg("ndim")) {
    nightDim = (server->arg("ndim") == "on");
    prefs.putBool("ndim", nightDim);
  }
  prefs.end();
  applyBacklight();
  drawScreen();
  server->send(200, "text/html", "<p>Saved. <a href=/settings>back</a></p>");
}

// Styled landing page: status + how to feed it (companion / pair), links.
static void handleRoot() {
  String ip = WiFi.localIP().toString();
  const char *st = selfHosted ? "Running self-contained"
                 : lastPushMs ? "Fed by the companion"
                              : "Not set up yet";
  String s = F(
      "<!DOCTYPE html><html><head><meta charset=utf-8>"
      "<meta name=viewport content='width=device-width,initial-scale=1'>"
      "<title>Headroom Mini</title><style>"
      "body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:22px 16px;margin:0}"
      ".card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;"
      "padding:18px;max-width:520px;margin:0 auto 14px}h1{margin:.1rem 0}"
      ".pill{display:inline-block;background:#efe9df;color:#6b6552;border-radius:999px;"
      "padding:3px 11px;font-size:.82rem}h3{margin:.2rem 0 .5rem}p{margin:.45rem 0}"
      "ol{padding-left:1.2rem;margin:.4rem 0}li{margin:.3rem 0}"
      "code{background:rgba(61,57,41,.07);padding:2px 6px;border-radius:6px;font-size:.9em;word-break:break-all}"
      "a.btn{display:inline-block;background:#d97757;color:#fff;text-decoration:none;"
      "font-weight:600;padding:11px 17px;border-radius:10px;margin:6px 8px 0 0}"
      ".muted{color:#94907e;font-size:.9rem}summary{cursor:pointer}</style></head><body>"
      "<div class=card><h1>Headroom Mini</h1><span class=pill>");
  s += st;
  s += F("</span></div><div class=card><h3>See your Claude usage</h3><ol>"
         "<li><b>Download the companion app</b> and open it.</li>"
         "<li>That's it &mdash; it finds this board on your network and shows "
         "your usage. It also starts with your computer so it stays live.</li>"
         "</ol><p><a class=btn href='https://daveeuson.github.io/HeadroomMini/'>"
         "Get the companion app</a></p>"
         "<p class=muted>No typing, no address to enter.</p></div>"
         "<div class=card><h3>Run without your computer <span class=muted>"
         "(optional)</span></h3>"
         "<p>Want the board to keep updating even when your computer is off? "
         "Open the companion once in <b>pair</b> mode &mdash; it hands the board "
         "your login and the board takes over:</p>"
         "<p><code>HeadroomCompanion --pair</code></p>"
         "<p class=muted>(finds this board automatically. From the source code: "
         "<code>python companion.py --pair</code>.) Tip: use a spare Claude "
         "account for the board.</p></div>"
         "<div class=card><a class=btn href=/alerts>Set up phone alerts</a>"
         "<a class=btn href=/settings style='background:#8a8577'>Settings</a>"
         "<details class=muted style='margin-top:12px'>"
         "<summary>Advanced: paste a login by hand</summary>"
         "<p><a href=/connect>Open the manual connect page</a> &mdash; only if "
         "you can't run the companion.</p></details></div>"
         "<p class=muted style='text-align:center'>");
  s += ip;
  s += F(" &middot; headroom.local</p></body></html>");
  server->send(200, "text/html", s);
}

// -------------------------------------------------------------- input helpers

static void toggleUsedMode() {
  showUsed = !showUsed;
  prefs.begin("headroom", false);
  prefs.putBool("used", showUsed);
  prefs.end();
  drawScreen();
}

// Wipe saved Wi-Fi + login and reboot into the setup portal.
static void factoryReset() {
  prefs.begin("headroom", false);
  prefs.clear();
  prefs.end();
  screenOff = false;
  setBacklight(255);
  gfx->fillScreen(C_BG);
  drawCentered("Reset", 130, 3, C_WARN);
  drawCentered("reconnect Wi-Fi to set up again", 175, 1, C_MUTED);
  delay(1500);
  ESP.restart();
}

// BOOT held ~5s -> factory reset. Cheap to poll every loop.
static void checkBootButton() {
  static unsigned long downSince = 0;
  if (digitalRead(BOOT_BTN) == LOW) {
    if (downSince == 0) downSince = millis();
    else if (millis() - downSince > 5000) factoryReset();
  } else {
    downSince = 0;
  }
}

// ------------------------------------------------------------- touch + motion
// Shared I2C bus (SDA 48 / SCL 47, 400 kHz): CST816D touch @ 0x15 (polled,
// no INT/RST), QMI8658 6-axis IMU @ 0x6B. Register maps verified against the
// Waveshare ESP-IDF demo + community drivers. Both degrade gracefully: if a
// chip isn't found, its feature is simply disabled.

static const int     I2C_SDA    = 48;
static const int     I2C_SCL    = 47;
static const uint8_t TOUCH_ADDR = 0x15;
static const uint8_t IMU_ADDR_A = 0x6B;   // Waveshare default (SA0 high)
static const uint8_t IMU_ADDR_B = 0x6A;   // fallback
static uint8_t imuAddr  = 0;
static bool    touchOk  = false;
static bool    imuOk    = false;

static bool i2cRead(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start
  if (Wire.requestFrom((int)addr, (int)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static void i2cWrite(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static void sensorsBegin() {
  Wire.begin(I2C_SDA, I2C_SCL, 400000);
  Wire.beginTransmission(TOUCH_ADDR);
  touchOk = (Wire.endTransmission() == 0);
  uint8_t imuCandidates[2] = {IMU_ADDR_A, IMU_ADDR_B};
  for (uint8_t a : imuCandidates) {
    uint8_t who = 0;
    if (i2cRead(a, 0x00, &who, 1) && who == 0x05) { imuAddr = a; imuOk = true; break; }
  }
  if (imuOk) {
    i2cWrite(imuAddr, 0x02, 0x60);   // CTRL1: addr auto-increment, little-endian
    i2cWrite(imuAddr, 0x03, 0x13);   // CTRL2: accel +/-4g  (8192 LSB/g)
    i2cWrite(imuAddr, 0x08, 0x01);   // CTRL7: accelerometer enable
  }
}

static void wake() { screenOff = false; applyBacklight(); }

static void cycleScreen(int dir) {
  uiScreen = (uiScreen + dir + UI_SCREENS) % UI_SCREENS;
  drawScreen();
}

static void bumpBrightness(int d) {
  int v = (int)backlight + d;
  if (v < 25) v = 25;
  if (v > 255) v = 255;
  screenOff = false;
  setBacklight((uint8_t)v);
}

// CST816 gesture codes: 1 up, 2 down, 3 left, 4 right, 5 tap, 0x0B dbl, 0x0C long
static void dispatchGesture(uint8_t g) {
  if (screenOff) { wake(); return; }        // a dimmed screen wakes on any touch
  switch (g) {
    case 0x0C: toggleUsedMode();   break;   // long press -> % left / % used
    case 0x01: bumpBrightness(+40); break;  // swipe up   -> brighter
    case 0x02: bumpBrightness(-40); break;  // swipe down -> dimmer
    case 0x03: cycleScreen(-1);    break;   // swipe left
    case 0x04: cycleScreen(+1);    break;   // swipe right
    default:   cycleScreen(+1);             // tap -> next screen
  }
}

// Poll the touch controller; dispatch on finger release using the strongest
// gesture seen during the press (handles tap / long-press / swipe uniformly).
static void pollTouch() {
  if (!touchOk) return;
  uint8_t b[6];
  if (!i2cRead(TOUCH_ADDR, 0x01, b, 6)) return;
  uint8_t gesture = b[0], finger = b[1];
  static bool touching = false;
  static uint8_t lastG = 0;
  if (finger == 1) {
    touching = true;
    if (gesture != 0) lastG = gesture;
  } else if (finger == 0 && touching) {
    dispatchGesture(lastG);                 // lastG 0 -> default tap
    touching = false;
    lastG = 0;
  }
}

// Accelerometer: flip-to-sleep and shake-to-wake. Self-calibrating — it takes
// "normal" from how the board is sitting at the first read, so which way the
// IMU's Z axis points (mounting-dependent on this board) doesn't matter.
static void pollMotion() {
  if (!imuOk) return;
  uint8_t b[6];
  if (!i2cRead(imuAddr, 0x35, b, 6)) return;
  float gx = (int16_t)((b[1] << 8) | b[0]) / 8192.0f;
  float gy = (int16_t)((b[3] << 8) | b[2]) / 8192.0f;
  float gz = (int16_t)((b[5] << 8) | b[4]) / 8192.0f;
  float mag = sqrtf(gx * gx + gy * gy + gz * gz);

  static bool  restInit = false;
  static float restZ = 1.0f;                 // gravity Z when sitting normally
  if (!restInit) { restZ = gz; restInit = true; return; }

  if (fabsf(mag - 1.0f) > 0.8f) {            // shake -> wake
    if (screenOff) wake();
    return;
  }
  if (fabsf(restZ) < 0.5f) return;           // boots upright -> don't auto-dim
  static int downCount = 0;
  float rel = gz * restZ;                     // >0 same as rest, <0 flipped over
  if (rel < -0.4f) {                          // flipped from its resting face
    if (++downCount > 3 && !screenOff) { screenOff = true; applyBacklight(); }
  } else if (rel > 0.3f) {                    // back to normal
    downCount = 0;
    if (screenOff) wake();
  }
}

// -------------------------------------------------------------------- setup

static void startPortal() {
  apMode = true;
  // AP+STA so WiFi.scanNetworks() can list nearby networks for the setup page
  // without dropping the phone off our hotspot.
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PSK);
  WiFi.disconnect();                            // STA idle, just here for scans
  dns.start(53, "*", WiFi.softAPIP());          // captive: everything -> us
  server = new WebServer(80);
  server->onNotFound(handlePortal);
  server->on("/", HTTP_GET, handlePortal);
  server->on("/scan", HTTP_GET, handleScan);
  server->on("/wifi", HTTP_POST, handleWifiSave);
  server->begin();
  gfx->fillScreen(C_BG);
  drawCentered("Let's connect", 34, 3, C_INK);
  // Primary path: the browser flasher provisions over USB (Improv).
  drawCentered("Setting up in your browser?", 84, 1, C_INK);
  drawCentered("Just enter your Wi-Fi there.", 104, 1, C_MUTED);
  // Fallback path: phone hotspot.
  drawCentered("- or from a phone -", 140, 1, C_MUTED);
  drawCentered("join Wi-Fi", 164, 1, C_MUTED);
  drawCentered(AP_SSID, 184, 2, C_ACC);
  drawCentered("password", 216, 1, C_MUTED);
  drawCentered(AP_PSK, 234, 3, C_INK);          // big so it's easy to read
  drawCentered("then open http://192.168.4.1", 282, 1, C_MUTED);
  improvSendState(improv::S_AUTHORIZED);        // announce we're ready
}

static void startApi() {
  server = new WebServer(API_PORT);
  server->on("/api/status", HTTP_GET, handleStatus);
  server->on("/api/push", HTTP_POST, handlePush);
  server->on("/api/pair", HTTP_POST, handlePair);
  server->on("/connect", HTTP_GET, handleConnectPage);
  server->on("/connect", HTTP_POST, handleConnectSave);
  server->on("/disconnect", HTTP_POST, handleDisconnect);
  server->on("/alerts", HTTP_GET, handleAlertsPage);
  server->on("/alerts", HTTP_POST, handleAlertsSave);
  server->on("/alerts/test", HTTP_POST, handleAlertsTest);
  server->on("/settings", HTTP_GET, handleSettingsPage);
  server->on("/settings", HTTP_POST, handleSettingsSave);
  server->on("/", HTTP_GET, handleRoot);
  server->begin();
  MDNS.begin("headroom");
  MDNS.addService("http", "tcp", API_PORT);
}

void setup() {
  Serial.begin(115200);
  pinMode(BOOT_BTN, INPUT_PULLUP);   // hold 5s -> factory reset Wi-Fi
  ledcSetup(BL_CHANNEL, 5000, 8);    // backlight PWM (active high on this board)
  ledcAttachPin(LCD_BL, BL_CHANNEL);
  setBacklight(255);
  gfx->begin(40000000);
  drawSplash("starting...", nullptr);
  sensorsBegin();                    // touch + IMU on the shared I2C bus

  prefs.begin("headroom", true);
  String ssid = prefs.getString("ssid", "");
  String psk = prefs.getString("psk", "");
  prefs.end();

  if (ssid.length() == 0) {
    startPortal();
    return;
  }

  drawSplash("joining Wi-Fi...", ssid.c_str());
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("headroom");
  WiFi.begin(ssid.c_str(), psk.c_str());
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 30000) delay(250);

  if (WiFi.status() != WL_CONNECTED) {
    // saved network unreachable -> offer setup again (don't wipe creds; a
    // router reboot shouldn't force reprovisioning — retry after portal boot)
    startPortal();
    drawCentered("(couldn't reach saved Wi-Fi)", 230, 1, C_WARN);
    return;
  }

  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  loadCreds();
  applyTz();          // header-clock timezone (from /settings; countdowns are TZ-free)
  loadHistory();
  readBattery();
  startApi();
  drawScreen();
  improvSendState(improv::S_PROVISIONED);   // in case the browser is listening
  pollUsage();                              // first live read if self-hosted
}

// --------------------------------------------------------------------- loop

void loop() {
  improvPoll();                     // browser can provision Wi-Fi over USB
  checkBootButton();                // hold BOOT 5s -> factory reset
  if (server) server->handleClient();
  if (apMode) {
    dns.processNextRequest();
    return;
  }
  static unsigned long lastTouch = 0, lastMotion = 0;
  if (millis() - lastTouch > 50)   { lastTouch = millis();  pollTouch(); }
  if (millis() - lastMotion > 400) { lastMotion = millis(); pollMotion(); }

  static unsigned long lastTick = 0;
  if (millis() - lastTick > 30000) {   // refresh clock/countdowns + battery
    lastTick = millis();
    if (!timeSynced && time(nullptr) > 1600000000) timeSynced = true;
    applyBacklight();     // ease down / back up as night comes and goes
    readBattery();
    drawScreen();
  }
  static unsigned long lastPoll = 0;   // self-hosted: pull fresh usage
  if (selfHosted && millis() - lastPoll > POLL_INTERVAL_MS) {
    lastPoll = millis();
    pollUsage();
  }
  static unsigned long lastSample = 0; // usage history ring buffer
  static int samplesSincePersist = 0;
  if (nWindows > 0 && (lastSample == 0 || millis() - lastSample > SAMPLE_INTERVAL_MS)) {
    lastSample = millis();
    sampleHistory();
    if (++samplesSincePersist >= 6) { samplesSincePersist = 0; saveHistory(); }
    if (uiScreen == 2) drawScreen();
  }
}
