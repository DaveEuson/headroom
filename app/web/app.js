/* ClaudeTrackerPi dashboard logic — meters show usage LEFT (fuel-gauge style). */

const POLL_MS = 30000;

let latest = null; // last /api/status payload
let night = { start: "22:00", end: "07:00" }; // overwritten from server config

// ?night=1 / ?night=0 forces night/day, ?active=1 / ?active=0 forces the
// session-detected state (both handy for testing)
const PARAMS = new URLSearchParams(location.search);
const FORCE_NIGHT = PARAMS.get("night");
const FORCE_ACTIVE = PARAMS.get("active");

function parseHM(text, fallback) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(text || "").trim());
  if (!m) return fallback;
  return (+m[1] % 24) * 60 + (+m[2] % 60);
}

function isNight() {
  if (FORCE_NIGHT !== null) return FORCE_NIGHT === "1";
  const start = parseHM(night.start, 22 * 60);
  const end = parseHM(night.end, 7 * 60);
  const d = new Date();
  const mins = d.getHours() * 60 + d.getMinutes();
  if (start === end) return false;
  return start < end ? mins >= start && mins < end
                     : mins >= start || mins < end; // window crosses midnight
}

const el = (id) => document.getElementById(id);

function severity(remaining) {
  if (remaining <= 10) return "crit";
  if (remaining <= 30) return "low";
  return "ok";
}

function stateText(sev) {
  return sev === "crit" ? "⚠ Almost out" : sev === "low" ? "▲ Running low" : "";
}

function fmtCountdown(ms) {
  if (ms <= 0) return "resetting…";
  const m = Math.floor(ms / 60000);
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const min = m % 60;
  if (d > 0) return `resets in ${d}d ${h}h`;
  if (h > 0) return `resets in ${h}h ${min}m`;
  return `resets in ${min}m`;
}

function fmtClock(date) {
  const opts = { hour: "numeric", minute: "2-digit" };
  const days = (date - new Date()) / 86400000;
  if (days > 22) return "";
  if (days > 0.9 || date.getDate() !== new Date().getDate()) {
    opts.weekday = "short";
    if (days > 6) { opts.month = "short"; opts.day = "numeric"; }
  }
  return date.toLocaleString([], opts);
}

function renderMeters(windows) {
  const box = el("meters");
  box.innerHTML = "";
  if (!windows.length) {
    box.innerHTML = '<p class="muted loading">No usage windows reported yet.</p>';
    return;
  }
  for (const w of windows) {
    const remaining = Math.max(0, Math.min(100, 100 - w.utilization));
    const sev = severity(remaining);
    const row = document.createElement("div");
    row.className = "window" + (sev === "ok" ? "" : " " + sev);
    row.dataset.resetsAt = w.resets_at || "";

    const shown = remaining < 1 && remaining > 0 ? remaining.toFixed(1)
                                                 : Math.round(remaining);
    row.innerHTML = `
      <div class="win-head">
        <span class="win-label"></span>
        <span class="win-value">${shown}<span class="unit">% left</span></span>
      </div>
      <div class="meter"><div class="fill" style="width:${remaining}%"></div></div>
      <div class="win-foot">
        <span class="reset"></span>
        <span class="state">${stateText(sev)}</span>
      </div>`;
    row.querySelector(".win-label").textContent = w.label;
    box.appendChild(row);
  }
  tickCountdowns();
}

function tickCountdowns() {
  document.querySelectorAll(".window").forEach((row) => {
    const iso = row.dataset.resetsAt;
    const target = row.querySelector(".reset");
    if (!iso) { target.textContent = ""; return; }
    const when = new Date(iso);
    if (isNaN(when)) { target.textContent = ""; return; }
    const clock = fmtClock(when);
    target.textContent = fmtCountdown(when - Date.now()) + (clock ? ` · ${clock}` : "");
  });
}

function renderBattery(battery) {
  const card = el("battery-card");
  if (!battery) { card.hidden = true; return; }
  card.hidden = false;
  const pct = Math.round(battery.percent);
  el("batt-pct").textContent = pct;
  el("batt-fill").style.width = battery.percent + "%";
  // note: the hidden attribute doesn't hide SVG elements, so toggle display
  el("batt-bolt").style.display =
    battery.charging || battery.plugged ? "" : "none";
  el("batt-note").textContent = battery.charging ? "Charging"
    : battery.plugged ? "On external power" : "On battery";
  card.classList.toggle("crit", pct <= 10);
  card.classList.toggle("low", pct > 10 && pct <= 25);
}

function sessionActive() {
  if (FORCE_ACTIVE !== null) return FORCE_ACTIVE === "1";
  return !!(latest && latest.session_active);
}

function renderMood(windows) {
  const mascot = el("mascot");
  // dances only while a session is being used; chills between sessions
  let mood = sessionActive() ? "happy" : "chill";
  if (isNight()) {
    mood = "sleep"; // Pip sleeps at night no matter what
  } else if (windows.length) {
    const minRemaining = Math.min(...windows.map((w) => 100 - w.utilization));
    if (minRemaining <= 10) mood = "panic";
    else if (minRemaining <= 30) mood = "worried";
  }
  const cls = "mascot mood-" + mood;
  if (mascot.className !== cls) mascot.className = cls;
}

function renderClock() {
  const now = new Date();
  el("clock-time").textContent =
    now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  el("clock-date").textContent =
    now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  document.body.classList.toggle("night", isNight());
}

function renderUpdated() {
  if (!latest) return;
  const target = el("updated");
  if (latest.usage_updated) {
    const secs = Math.max(0, Math.round(Date.now() / 1000 - latest.usage_updated));
    target.textContent = secs < 5 ? "updated just now"
      : secs < 120 ? `updated ${secs}s ago`
      : `updated ${Math.round(secs / 60)}m ago`;
  } else {
    target.textContent = "waiting for first reading…";
  }
}

function render(data) {
  latest = data;
  // Anchor "updated x ago" to our clock, not the Pi's (they may disagree).
  if (data.usage_updated && data.server_time) {
    latest.usage_updated = Date.now() / 1000 - (data.server_time - data.usage_updated);
  }
  const banner = el("error");
  banner.hidden = !data.usage_error;
  if (data.usage_error) banner.textContent = data.usage_error;

  const plan = el("plan");
  plan.hidden = !data.plan;
  if (data.plan) plan.textContent = "Claude " + data.plan;

  if (data.night) night = data.night;

  renderMeters(data.windows || []);
  renderMood(data.windows || []);
  renderBattery(data.battery);
  renderUpdated();
}

async function poll() {
  try {
    const resp = await fetch("/api/status", { cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    render(await resp.json());
  } catch (err) {
    const banner = el("error");
    banner.hidden = false;
    banner.textContent = "Can't reach the tracker on the Pi (" + err.message + "). Retrying…";
  }
}

poll();
renderClock();
setInterval(poll, POLL_MS);
setInterval(() => {
  tickCountdowns();
  renderUpdated();
  renderClock();
  if (latest) renderMood(latest.windows || []); // day/night flips between polls
}, 1000);
