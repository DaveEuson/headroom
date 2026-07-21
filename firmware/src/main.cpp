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
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <ArduinoJson.h>
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
// rotation 0 = portrait 240x320, IPS panel
static Arduino_GFX *gfx =
    new Arduino_ST7789(bus, LCD_RST, 0 /*rotation*/, true /*IPS*/, 240, 320);

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
static const char *AP_PSK  = "claudepi";
static const int   API_PORT = 8080;   // what the companion probes

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
    bool stale = lastPushMs == 0;
    drawCentered(stale ? "Waiting for the companion" : "No usage windows",
                 150, 1, C_MUTED);
    drawCentered("run it on your computer with", 170, 1, C_MUTED);
    snprintf(buf, sizeof(buf), "--pi http://%s:%d",
             WiFi.localIP().toString().c_str(), API_PORT);
    drawCentered(buf, 190, 1, C_ACC);
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

    snprintf(buf, sizeof(buf), "%d%% left", (int)(left + 0.5f));
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
    gfx->print("stale - companion quiet >10m");
  } else {
    gfx->print(WiFi.localIP().toString());
    gfx->print("  headroom.local");
  }
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
  drawMeters();
}

// Wi-Fi provisioning portal (AP mode)
static const char PORTAL_HTML[] PROGMEM = R"HTML(<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Headroom Wi-Fi setup</title>
<style>body{font-family:system-ui;background:#f0eee6;color:#3d3929;padding:24px 18px}
.card{background:#faf9f5;border:1px solid rgba(61,57,41,.12);border-radius:14px;padding:16px;max-width:420px;margin:0 auto}
input{width:100%;padding:12px;font-size:1rem;border-radius:10px;border:1px solid rgba(61,57,41,.25);margin:6px 0 12px;box-sizing:border-box}
button{display:block;width:100%;background:#d97757;color:#fff;font-weight:600;font-size:1.05rem;padding:14px;border-radius:10px;border:none}</style>
</head><body><div class="card">
<h2>Connect Headroom to Wi-Fi</h2>
<p>Enter your home network. The device will reboot and join it.</p>
<form method="POST" action="/wifi">
<input name="ssid" placeholder="Network name (SSID)" autocapitalize="off">
<input name="password" type="password" placeholder="Password">
<button type="submit">Connect</button></form>
</div></body></html>)HTML";

static void handlePortal() { server->send(200, "text/html", PORTAL_HTML); }

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

// -------------------------------------------------------------------- setup

static void startPortal() {
  apMode = true;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PSK);
  dns.start(53, "*", WiFi.softAPIP());          // captive: everything -> us
  server = new WebServer(80);
  server->onNotFound(handlePortal);
  server->on("/", HTTP_GET, handlePortal);
  server->on("/wifi", HTTP_POST, handleWifiSave);
  server->begin();
  gfx->fillScreen(C_BG);
  drawCentered("Wi-Fi setup", 60, 3, C_INK);
  drawCentered("On your phone, join:", 130, 1, C_MUTED);
  drawCentered(AP_SSID, 150, 2, C_ACC);
  char buf[40];
  snprintf(buf, sizeof(buf), "password: %s", AP_PSK);
  drawCentered(buf, 180, 1, C_INK);
  drawCentered("then open http://192.168.4.1", 205, 1, C_MUTED);
}

static void startApi() {
  server = new WebServer(API_PORT);
  server->on("/api/status", HTTP_GET, handleStatus);
  server->on("/api/push", HTTP_POST, handlePush);
  server->on("/", HTTP_GET, []() {
    server->send(200, "text/html",
                 "<h1>Headroom Mini</h1><p>POST /api/push feeds this display. "
                 "Run the Headroom companion with --pi http://" +
                     WiFi.localIP().toString() + ":8080</p>");
  });
  server->begin();
  MDNS.begin("headroom");
  MDNS.addService("http", "tcp", API_PORT);
}

void setup() {
  Serial.begin(115200);
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);        // backlight on (active high on this board)
  gfx->begin(40000000);
  drawSplash("starting...", nullptr);

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
  startApi();
  drawMeters();
}

// --------------------------------------------------------------------- loop

void loop() {
  if (server) server->handleClient();
  if (apMode) {
    dns.processNextRequest();
    return;
  }
  static unsigned long lastTick = 0;
  if (millis() - lastTick > 30000) {   // refresh clock/countdowns
    lastTick = millis();
    if (!timeSynced && time(nullptr) > 1600000000) timeSynced = true;
    drawMeters();
  }
}
