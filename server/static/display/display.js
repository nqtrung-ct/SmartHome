// Wall display: dashboard + điều khiển thiết bị sau khi nhập mã PIN.
"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- unlock state
// token + thời điểm hết hạn (5 phút) — chỉ nằm trong bộ nhớ của trang
let unlock = { token: null, until: 0 };

const unlocked = () => unlock.token && Date.now() < unlock.until;

function tickUnlock() {
  const ind = $("lock-ind");
  if (unlocked()) {
    const left = Math.max(0, Math.floor((unlock.until - Date.now()) / 1000));
    ind.textContent = `🔓 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    ind.classList.add("on");
  } else {
    if (unlock.token) { unlock = { token: null, until: 0 }; }
    ind.textContent = "🔒";
    ind.classList.remove("on");
  }
}

// ---------------------------------------------------------------- clock
const WMO_ICON = (code) => {
  if (code <= 1) return code === 0 ? "☀️" : "🌤️";
  if (code <= 3) return code === 2 ? "⛅" : "☁️";
  if (code <= 48) return "🌫️";
  if (code <= 67) return "🌧️";
  if (code <= 77) return "🌨️";
  if (code <= 82) return "🌦️";
  return "⛈️";
};

function tick() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("vi-VN", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const hour = now.getHours();
  $("greeting").textContent =
    hour < 11 ? "Chào buổi sáng" : hour < 14 ? "Chào buổi trưa" :
    hour < 18 ? "Chào buổi chiều" : "Chào buổi tối";
  $("dateline").textContent = now.toLocaleDateString("vi-VN", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
  tickUnlock();
}
tick();
setInterval(tick, 1000);

$("fullscreen").onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen();
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- data
let lastData = { rooms: [] };

async function refresh() {
  let data;
  try {
    const resp = await fetch("/api/display");
    if (!resp.ok) throw new Error(resp.status);
    data = await resp.json();
    lastData = data;
  } catch {
    return; // giữ dữ liệu cũ trên màn hình; thử lại chu kỳ sau
  }

  // weather
  const w = data.weather;
  if (w) {
    $("w-icon").textContent = WMO_ICON(w.code);
    $("w-temp").textContent = `${w.temperature}°`;
    $("w-cond").textContent = w.condition.replace(/^./, (c) => c.toUpperCase());
    $("w-wind").textContent = w.wind;
    $("w-hum").textContent = w.humidity;
    $("w-rain").textContent = w.rain_prob ?? "–";
    $("w-range").textContent = `cao nhất ${w.temp_max}° · thấp nhất ${w.temp_min}°`;
  } else {
    $("w-cond").textContent = "không lấy được thời tiết";
  }

  renderRooms(data.rooms);
  renderTimers(data.timers);
}

// ---------------------------------------------------------------- room cards
const TYPE_ICON = { light: "💡", switch: "🔌", climate: "❄️" };

// thiết bị IR không phản hồi trạng thái thật — chỉ hiện tên, không hiện bật/tắt
function chipState(e) {
  if (e.driver === "broadlink") return "";
  if (e.type === "climate") {
    const on = e.state && e.state.state === "on";
    return on && e.state.temperature != null ? `${e.state.temperature}°C` : "đang tắt";
  }
  return e.state && e.state.state === "on" ? "đang bật" : "đang tắt";
}

function renderRooms(rooms) {
  const el = $("room-grid");
  if (!rooms.length) {
    el.innerHTML = `<p class="muted">Chưa có phòng nào có cảm biến nhiệt độ.</p>`;
    return;
  }
  el.innerHTML = rooms.map((r) => `
    <div class="room-card card">
      <div class="room-head">
        <div>
          <span class="room-name">${esc(r.name)}</span>
          ${r.floor ? `<span class="badge">${esc(r.floor)}</span>` : ""}
        </div>
        <div class="room-env">
          ${r.temperature != null ? `<span class="room-temp">${r.temperature}°C</span>` : ""}
          ${r.humidity != null ? `<span class="room-hum">💧 ${r.humidity}%</span>` : ""}
        </div>
      </div>
      <div class="chip-row">
        ${(r.entities || []).map((e) => {
          const ctl = e.controllable ? " ctl" : "";
          const ir = e.driver === "broadlink" ? `<span class="ir-tag">IR</span>` : "";
          const st = chipState(e);
          return `<div class="chip${ctl}${st === "đang bật" ? " on" : ""}" data-ent="${e.id}">
            <span class="chip-ic">${TYPE_ICON[e.type] || "🔘"}</span>
            <span class="chip-body">
              <span class="chip-name">${esc(e.name)}${ir}</span>
              ${st ? `<span class="chip-st">${st}</span>` : ""}
            </span>
          </div>`;
        }).join("") || `<span class="muted small">phòng chưa có thiết bị điều khiển</span>`}
      </div>
    </div>`).join("");
  el.querySelectorAll(".chip.ctl").forEach((c) =>
    c.addEventListener("click", () => openCtrl(+c.dataset.ent)));
}

function findEntity(id) {
  for (const r of lastData.rooms) {
    const e = (r.entities || []).find((x) => x.id === id);
    if (e) return { e, r };
  }
  return { e: null, r: null };
}

// ---------------------------------------------------------------- PIN unlock
// PIN chỉ hiện khi bấm vào một thiết bị điều khiển được (chặn trẻ em vọc),
// không có nút "mở khóa chung" — mở đúng lúc đó, hết 5 phút thì hỏi lại.
function showPin(cb, deviceName) {
  $("pin-error").textContent = "";
  $("pin-input").value = "";
  $("pin-sub").textContent = deviceName
    ? `Nhập mã PIN 4 số để điều khiển "${deviceName}" (hiệu lực 5 phút)`
    : "Nhập mã PIN 4 số (hiệu lực 5 phút)";
  $("pin-overlay").classList.remove("hidden");
  $("pin-input").focus();
  pinCb = cb;
}
let pinCb = null;

async function tryPin() {
  const pin = $("pin-input").value.trim();
  if (!/^\d{4}$/.test(pin)) {
    $("pin-error").textContent = "Mã gồm 4 chữ số";
    return;
  }
  $("pin-ok").disabled = true;
  try {
    const resp = await fetch("/api/display/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      $("pin-error").textContent = d.detail || "Mã PIN không đúng";
      return;
    }
    const d = await resp.json();
    unlock = { token: d.token, until: Date.now() + d.expires_in * 1000 };
    $("pin-overlay").classList.add("hidden");
    tickUnlock();
    if (pinCb) { const cb = pinCb; pinCb = null; cb(); }
  } catch {
    $("pin-error").textContent = "Không kết nối được";
  } finally {
    $("pin-ok").disabled = false;
  }
}

$("pin-ok").onclick = tryPin;
$("pin-input").addEventListener("keydown", (ev) => { if (ev.key === "Enter") tryPin(); });
$("pin-cancel").onclick = () => { pinCb = null; $("pin-overlay").classList.add("hidden"); };

// ---------------------------------------------------------------- control panel
let ctrlTemp = 26;

function ctrlButtons(e) {
  if (e.type === "climate") {
    ctrlTemp = e.state && e.state.temperature ? e.state.temperature : 26;
    return `
      <div class="temp-row">
        <button class="btn" data-cmd="temp-">−</button>
        <span id="ctrl-temp" class="temp-num">${ctrlTemp}°C</span>
        <button class="btn" data-cmd="temp+">＋</button>
      </div>
      <div class="row-center">
        <button class="btn primary" data-cmd="on">Bật</button>
        <button class="btn danger" data-cmd="off">Tắt</button>
      </div>`;
  }
  return `
    <div class="row-center">
      <button class="btn primary" data-cmd="on">Bật</button>
      <button class="btn danger" data-cmd="off">Tắt</button>
    </div>`;
}

function openCtrl(id) {
  const { e, r } = findEntity(id);
  if (!e) return;
  if (!unlocked()) { showPin(() => openCtrl(id), e.name); return; }
  $("ctrl-icon").textContent = TYPE_ICON[e.type] || "🔘";
  $("ctrl-name").textContent = e.name;
  $("ctrl-room").textContent = r ? r.name : "";
  $("ctrl-body").innerHTML = ctrlButtons(e);
  $("ctrl-body").querySelectorAll("[data-cmd]").forEach((b) =>
    b.addEventListener("click", () => doCommand(e, b.dataset.cmd)));
  $("ctrl-overlay").classList.remove("hidden");
}

$("ctrl-close").onclick = () => $("ctrl-overlay").classList.add("hidden");

async function doCommand(e, cmd) {
  if (!unlocked()) { $("ctrl-overlay").classList.add("hidden"); showPin(() => openCtrl(e.id)); return; }
  let command = cmd, params = {};
  if (cmd === "temp-" || cmd === "temp+") {
    ctrlTemp = Math.min(30, Math.max(16, ctrlTemp + (cmd === "temp+" ? 1 : -1)));
    $("ctrl-temp").textContent = `${ctrlTemp}°C`;
    return; // chỉnh số trước, bấm Bật mới gửi
  }
  if (cmd === "on" && e.type === "climate") {
    command = "set";
    params = { mode: "cool", temperature: ctrlTemp };
  }
  try {
    const resp = await fetch("/api/display/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: unlock.token, entity: e.id, command, params }),
    });
    if (resp.status === 401) {
      unlock = { token: null, until: 0 };
      $("ctrl-overlay").classList.add("hidden");
      showPin(null);
      return;
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      alert(d.detail || "Không điều khiển được");
      return;
    }
  } catch {
    alert("Không kết nối được");
    return;
  }
  refresh();
}

// ---------------------------------------------------------------- timers
function renderTimers(timers) {
  $("timer-list").innerHTML = timers.length
    ? timers.map((t) => {
        const at = new Date(t.target_at);
        const hhmm = at.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", hour12: false });
        const day = at.toLocaleDateString("vi-VN", { day: "numeric", month: "numeric" });
        return `<div class="timer">
          <div class="t-time">${hhmm}<div class="t-room">${day}</div></div>
          <div><div class="t-msg">${esc(t.message)}</div>
          ${t.room ? `<div class="t-room">${esc(t.room)}</div>` : ""}</div>
        </div>`;
      }).join("")
    : `<p class="muted">Không có hẹn giờ.</p>`;
}

refresh();
setInterval(refresh, 30000);
