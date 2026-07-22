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
static const char *FW_VERSION = "0.1";

// Phase 2 — self-contained: poll Anthropic's usage endpoint directly, using an
// OAuth login pasted once via /connect. Same contract the companion uses.
static const char *CLIENT_ID   = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
static const char *REFRESH_URL = "https://platform.claude.com/v1/oauth/token";
static const char *USAGE_URL   = "https://api.anthropic.com/api/oauth/usage";
static const char *OAUTH_BETA  = "oauth-2025-04-20";
static const char *UA          = "Headroom-Mini/0.1";
static const unsigned long POLL_INTERVAL_MS = 5UL * 60UL * 1000UL;

static String   accessTok, refreshTok;
static uint64_t tokenExpMs = 0;       // epoch ms, 0 = unknown
static bool     selfHosted = false;   // true once a login is stored

// UI / input state (Phase 1.5)
static const int BL_CHANNEL = 0;      // LEDC channel for backlight PWM
static const int BOOT_BTN    = 0;     // BOOT button -> hold to factory reset
static const int BAT_ADC_PIN = 5;     // VBAT via 200K/100K divider (x3), ADC1_CH4
static int       batPct      = -1;    // -1 = no battery / hidden
static bool      batCharging = false;
static uint8_t   backlight   = 255;   // 0..255
static bool      showUsed    = false; // false = "% left", true = "% used"
static bool      screenOff   = false; // face-down / manual dim
static int       uiScreen    = 0;     // 0 = all meters, 1 = focus, 2 = history
static const int UI_SCREENS  = 3;

// Usage history: a ring buffer of the headline utilization, one sample every
// SAMPLE_INTERVAL_MS, persisted to flash hourly so it survives reboots.
static const int HIST_LEN = 60;
static uint8_t   histBuf[HIST_LEN];
static int       histCount = 0;       // valid samples so far (<= HIST_LEN)
static int       histHead  = 0;       // ring write index
static const unsigned long SAMPLE_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 min

static void setBacklight(uint8_t v) {
  backlight = v;
  ledcWrite(BL_CHANNEL, screenOff ? 0 : v);
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

  // header: clock (UTC-less fallback: hide until NTP syncs)
  char buf[40];
  time_t now = time(nullptr);
  if (timeSynced && now > 100000) {
    struct tm tmnow;
    localtime_r(&now, &tmnow);
    strftime(buf, sizeof(buf), "%H:%M", &tmnow);
    gfx->setTextSize(4);
    gfx->setTextColor(C_INK);
    gfx->setCursor(10, 10);
    gfx->print(buf);
  } else {
    gfx->setTextSize(2);
    gfx->setTextColor(C_MUTED);
    gfx->setCursor(10, 16);
    gfx->print("--:--");
  }
  if (plan[0]) {
    snprintf(buf, sizeof(buf), "%s", plan);
    gfx->setTextSize(1);
    gfx->setTextColor(C_ACC);
    gfx->setCursor(190, 14);
    gfx->print(buf);
  }

  if (nWindows == 0) {
    if (selfHosted) {
      drawCentered("Fetching your usage...", 150, 1, C_MUTED);
    } else if (lastPushMs == 0) {
      drawCentered("Connect your account:", 140, 1, C_MUTED);
      snprintf(buf, sizeof(buf), "http://%s/connect",
               WiFi.localIP().toString().c_str());
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

// Draw whichever screen is active (data updates / ticks call this).
static void drawScreen() {
  if (uiScreen == 1)      drawFocus();
  else if (uiScreen == 2) drawHistory();
  else                    drawMeters();
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
  prefs.end();
  selfHosted = accessTok.length() > 0;
}

static void saveCreds() {
  prefs.begin("headroom", false);
  prefs.putString("atok", accessTok);
  prefs.putString("rtok", refreshTok);
  prefs.putULong64("exp", tokenExpMs);
  prefs.putString("plan", plan);
  prefs.end();
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
  if (accessTok.length() == 0) return false;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient https;
  if (!https.begin(client, USAGE_URL)) return false;
  https.addHeader("Authorization", "Bearer " + accessTok);
  https.addHeader("anthropic-beta", OAUTH_BETA);
  https.addHeader("Accept", "application/json");
  https.addHeader("User-Agent", UA);
  int code = https.GET();
  if ((code == 401 || code == 403) && allowRefresh) {
    https.end();
    return refreshAccess() ? fetchUsage(false) : false;
  }
  if (code != 200) { https.end(); return false; }
  String payload = https.getString();
  https.end();

  JsonDocument doc;
  if (deserializeJson(doc, payload)) return false;
  JsonObject root = doc.as<JsonObject>();
  if (root.isNull()) return false;

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
  if (nWindows == 0) return false;
  lastPushMs = millis();
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
  if (raw.length() == 0 || deserializeJson(doc, raw)) {
    server->send(200, "text/html",
                 "<p>Couldn't read that JSON. <a href=/connect>back</a></p>");
    return;
  }
  JsonObject o = doc["claudeAiOauth"].is<JsonObject>()
                     ? doc["claudeAiOauth"].as<JsonObject>()
                     : doc.as<JsonObject>();
  const char *at = o["accessToken"].as<const char *>();
  if (!at) at = o["access_token"].as<const char *>();
  if (!at) {
    server->send(200, "text/html",
                 "<p>No access token in that file. <a href=/connect>back</a></p>");
    return;
  }
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

static void wake() { screenOff = false; setBacklight(backlight); }

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

// Accelerometer: face-down dims, face-up restores, a shake wakes.
static void pollMotion() {
  if (!imuOk) return;
  uint8_t b[6];
  if (!i2cRead(imuAddr, 0x35, b, 6)) return;
  float gx = (int16_t)((b[1] << 8) | b[0]) / 8192.0f;
  float gy = (int16_t)((b[3] << 8) | b[2]) / 8192.0f;
  float gz = (int16_t)((b[5] << 8) | b[4]) / 8192.0f;
  float mag = sqrtf(gx * gx + gy * gy + gz * gz);

  if (fabsf(mag - 1.0f) > 0.8f) {           // shake
    if (screenOff) wake();
    return;
  }
  static int downCount = 0;
  if (gz < -0.6f) {                          // face down
    if (++downCount > 3 && !screenOff) { screenOff = true; setBacklight(backlight); }
  } else if (gz > 0.2f) {                    // face up again
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
  server->on("/connect", HTTP_GET, handleConnectPage);
  server->on("/connect", HTTP_POST, handleConnectSave);
  server->on("/disconnect", HTTP_POST, handleDisconnect);
  server->on("/", HTTP_GET, []() {
    server->send(200, "text/html",
                 "<h1>Headroom Mini</h1>"
                 "<p><b><a href=/connect>Connect your Claude account</a></b> to "
                 "run self-contained (no computer needed).</p>"
                 "<p>Or feed it from a computer: run the Headroom companion with "
                 "--pi http://" + WiFi.localIP().toString() + ":8080</p>");
  });
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
  // TZ for the header clock; countdowns are TZ-independent. Adjust to taste.
  setenv("TZ", "EST5EDT,M3.2.0,M11.1.0", 1);
  tzset();
  loadCreds();
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
