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
  improvSendState(improv::S_PROVISIONED);   // in case the browser is listening
}

// --------------------------------------------------------------------- loop

void loop() {
  improvPoll();                     // browser can provision Wi-Fi over USB
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
