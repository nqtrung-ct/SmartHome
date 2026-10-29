// Wall display: dashboard + điều khiển thiết bị sau khi nhập mã PIN.
"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- unlock state
// token + thời điểm hết hạn (5 phút) — chỉ nằm trong bộ nhớ của trang
let unlock = { token: null, until: 0 };

const unlocked = () => unlock.token && Date.now() < unlock.until;

// emoji khóa là emoji màu (vàng) — dùng SVG trắng cho hợp nền tối
const LOCK_SVG = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="4" y="10.5" width="16" height="10.5" rx="2.5"/><path d="M8 10.5V7a4 4 0 1 1 8 0v3.5"/></svg>`;
const UNLOCK_SVG = `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="4" y="10.5" width="16" height="10.5" rx="2.5"/><path d="M8 10.5V7a4 4 0 0 1 7.6-1.7"/></svg>`;

function tickUnlock() {
  const ind = $("lock-ind");
  if (unlocked()) {
    const left = Math.max(0, Math.floor((unlock.until - Date.now()) / 1000));
    ind.innerHTML = `${UNLOCK_SVG} ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    ind.classList.add("on");
  } else {
    if (unlock.token) { unlock = { token: null, until: 0 }; }
    ind.innerHTML = LOCK_SVG;
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

// ---------------------------------------------------------------- âm lịch
// Thuật toán lịch âm Việt Nam (bản Hồ Ngọc Đức) — tính thuần toán, không cần
// mạng; chính xác khoảng 1800-2199. Múi giờ Việt Nam UTC+7.

function jdFromDate(dd, mm, yy) {
  const a = Math.floor((14 - mm) / 12);
  const y = yy + 4800 - a, m = mm + 12 * a - 3;
  let jd = dd + Math.floor((153 * m + 2) / 5) + 365 * y + Math.floor(y / 4)
    - Math.floor(y / 100) + Math.floor(y / 400) - 32045;
  if (jd < 2299161) {
    jd = dd + Math.floor((153 * m + 2) / 5) + 365 * y + Math.floor(y / 4) - 32083;
  }
  return jd;
}

function jdToDate(jd) {
  let a, b, c, d, e, m;
  if (jd > 2299160) {
    a = jd + 32044; b = Math.floor((4 * a + 3) / 146097); c = a - Math.floor((b * 146097) / 4);
  } else { b = 0; c = jd + 32082; }
  d = Math.floor((4 * c + 3) / 1461); e = c - Math.floor((1461 * d) / 4);
  m = Math.floor((5 * e + 2) / 153);
  return [e - Math.floor((153 * m + 2) / 5) + 1, m + 3 - 12 * Math.floor(m / 10),
          b * 100 + d - 4800 + Math.floor(m / 10)];
}

function newMoon(k) {
  const T = k / 1236.85, T2 = T * T, T3 = T2 * T, dr = Math.PI / 180;
  let Jd1 = 2415020.75933 + 29.53058868 * k + 0.0001178 * T2 - 0.000000155 * T3;
  Jd1 = Jd1 + 0.00033 * Math.sin((166.56 + 132.87 * T - 0.009173 * T2) * dr);
  const M = 359.2242 + 29.10535608 * k - 0.0000333 * T2 - 0.00000347 * T3;
  const Mpr = 306.0253 + 385.81691806 * k + 0.0107306 * T2 + 0.00001236 * T3;
  const F = 21.2964 + 390.67050646 * k - 0.0016528 * T2 - 0.00000239 * T3;
  let C1 = (0.1734 - 0.000393 * T) * Math.sin(M * dr);
  C1 = C1 + 0.0021 * Math.sin(2 * dr * M);
  C1 = C1 - 0.4068 * Math.sin(Mpr * dr);
  C1 = C1 + 0.0161 * Math.sin(dr * 2 * Mpr);
  C1 = C1 - 0.0004 * Math.sin(dr * 3 * Mpr);
  C1 = C1 + 0.0104 * Math.sin(dr * 2 * F);
  C1 = C1 - 0.0051 * Math.sin(dr * (M + Mpr));
  C1 = C1 - 0.0074 * Math.sin(dr * (M - Mpr));
  C1 = C1 + 0.0004 * Math.sin(dr * (2 * F + M));
  C1 = C1 - 0.0004 * Math.sin(dr * (2 * F - M));
  C1 = C1 - 0.0006 * Math.sin(dr * (2 * F + Mpr));
  C1 = C1 + 0.001 * Math.sin(dr * (2 * F - Mpr));
  C1 = C1 + 0.0005 * Math.sin(dr * (2 * Mpr + M));
  let deltat;
  if (T < -11) {
    deltat = 0.001 + 0.000839 * T + 0.0002261 * T2 - 0.00000845 * T3 - 0.000000081 * T * T3;
  } else {
    deltat = -0.000278 + 0.000265 * T + 0.000262 * T2;
  }
  return Jd1 + C1 - deltat;
}

function sunLongitude(jdn) {
  const T = (jdn - 2451545.0) / 36525, T2 = T * T, dr = Math.PI / 180;
  const M = 357.52910 + 35999.05030 * T - 0.0001559 * T2 - 0.00000048 * T * T2;
  const L0 = 280.46645 + 36000.76983 * T + 0.0003032 * T2;
  let DL = (1.914600 - 0.004817 * T - 0.000014 * T2) * Math.sin(dr * M);
  DL += (0.019993 - 0.000101 * T) * Math.sin(2 * M * dr) + 0.000290 * Math.sin(3 * M * dr);
  let L = (L0 + DL) * dr;
  return L - Math.PI * 2 * Math.floor(L / (Math.PI * 2));
}

function getNewMoonDay(k, timeZone) {
  // làm tròn ngày trăng thiên văn về số nguyên Julian theo giờ địa phương
  return Math.floor(newMoon(k) + 0.5 + timeZone / 24);
}

function getSunLongitude(dayNumber, timeZone) {
  return Math.floor(sunLongitude(dayNumber - 0.5 - timeZone / 24) / Math.PI * 6);
}

function getLunarMonth11(yy, timeZone) {
  const off = jdFromDate(31, 12, yy) - 2415021;
  const k = Math.floor(off / 29.530588853);
  let nm = getNewMoonDay(k, timeZone);
  if (getSunLongitude(nm, timeZone) >= 9) nm = getNewMoonDay(k - 1, timeZone);
  return nm;
}

function getLeapMonthOffset(a11, timeZone) {
  const k = Math.floor((a11 - 2415021.076998695) / 29.530588853 + 0.5);
  let last = 0, i = 1, arc = getSunLongitude(getNewMoonDay(k + i, timeZone), timeZone);
  do {
    last = arc; i++;
    arc = getSunLongitude(getNewMoonDay(k + i), timeZone);
  } while (arc !== last && i < 14);
  return i - 1;
}

function solar2Lunar(dd, mm, yy, timeZone) {
  const dayNumber = jdFromDate(dd, mm, yy);
  const k = Math.floor((dayNumber - 2415021.076998695) / 29.530588853);
  let monthStart = getNewMoonDay(k + 1, timeZone);
  if (monthStart > dayNumber) monthStart = getNewMoonDay(k, timeZone);
  let a11 = getLunarMonth11(yy, timeZone), b11 = a11, lunarYear;
  if (a11 >= monthStart) {
    lunarYear = yy; a11 = getLunarMonth11(yy - 1, timeZone);
  } else {
    lunarYear = yy + 1; b11 = getLunarMonth11(yy + 1, timeZone);
  }
  const lunarDay = dayNumber - monthStart + 1;
  const diff = Math.floor((monthStart - a11) / 29);
  let lunarLeap = 0, lunarMonth = diff + 11;
  if (b11 - a11 > 365) {
    const leapDiff = getLeapMonthOffset(a11, timeZone);
    if (diff >= leapDiff) {
      lunarMonth = diff + 10;
      if (diff === leapDiff) lunarLeap = 1;
    }
  }
  if (lunarMonth > 12) lunarMonth -= 12;
  if (lunarMonth >= 11 && diff < 4) lunarYear -= 1;
  return [lunarDay, lunarMonth, lunarYear, lunarLeap];
}

const CAN = ["Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý"];
const CHI = ["Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi"];

function lunarText(now) {
  const [ld, lm, ly, leap] = solar2Lunar(now.getDate(), now.getMonth() + 1, now.getFullYear(), 7);
  return `Âm lịch: ${ld} tháng ${lm}${leap ? " (nhuận)" : ""} năm ${CAN[(ly + 6) % 10]} ${CHI[(ly + 8) % 12]}`;
}

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
  $("dateline-lunar").textContent = lunarText(now);
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
    $("w-range").textContent = `${w.temp_min}° - ${w.temp_max}°`;
  } else {
    $("w-cond").textContent = "không lấy được thời tiết";
  }

  // cảnh báo điện lưới: tắt = đỏ; bật mà Vol < 200 = vàng
  const gw = $("grid-warn"), gt = $("grid-warn-text"), g = data.grid;
  if (g && g.on === false) {
    gt.textContent = "Mất điện lưới";
    gw.className = "grid-warn red";
  } else if (g && g.on && g.low_voltage) {
    gt.textContent = `Điện yếu (${Math.round(g.voltage)}V)`;
    gw.className = "grid-warn yellow";
  } else {
    gw.className = "grid-warn hidden";
  }

  renderRooms(data.rooms);
  renderTimers(data.timers);
}

// ---------------------------------------------------------------- room cards
const TYPE_ICON = { light: "💡", switch: "🔌", climate: "❄️", fan: "🌀", media: "🔊" };

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
  // mặc định 1 cột rộng (span cả vùng phải); chỉ khi không đủ chỗ dọc mới tách 2 cột
  el.classList.remove("two");
  el.classList.add("one");
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
  // đo sau khi DOM có thật: card phòng tràn khỏi màn hình thì chuyển 2 cột
  requestAnimationFrame(() => {
    const top = el.getBoundingClientRect().top;
    if (top > 0 && el.scrollHeight > window.innerHeight - top - 24) {
      el.classList.replace("one", "two");
    }
  });
}

function findEntity(id) {
  for (const r of lastData.rooms) {
    const e = (r.entities || []).find((x) => x.id === id);
    if (e) return { e, r };
  }
  return { e: null, r: null };
}

// ---------------------------------------------------------------- PIN unlock
// PIN chỉ hiện khi bấm vào một thiết bị điều khiển được (chặn trẻ em vọc);
// nhập đủ 4 số là tự xác nhận. Đúng một lần -> mọi thiết bị mở trong 5 phút.
function showPin(cb, deviceName) {
  $("pin-error").textContent = "";
  $("pin-input").value = "";
  $("pin-sub").textContent = deviceName
    ? `Nhập mã PIN 4 số để điều khiển "${deviceName}"`
    : "Nhập mã PIN 4 số";
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
  try {
    const resp = await fetch("/api/display/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      $("pin-error").textContent = d.detail || "Mã PIN không đúng";
      $("pin-input").value = "";
      $("pin-input").focus();
      return;
    }
    const d = await resp.json();
    unlock = { token: d.token, until: Date.now() + d.expires_in * 1000 };
    $("pin-overlay").classList.add("hidden");
    tickUnlock();
    if (pinCb) { const cb = pinCb; pinCb = null; cb(); }
  } catch {
    $("pin-error").textContent = "Không kết nối được";
  }
}

$("pin-input").addEventListener("input", () => {
  // đủ 4 số là tự xác nhận, không cần bấm nút
  if ($("pin-input").value.length === 4) tryPin();
});
$("pin-input").addEventListener("keydown", (ev) => { if (ev.key === "Enter") tryPin(); });
$("pin-clear").onclick = () => {
  $("pin-input").value = "";
  $("pin-error").textContent = "";
  $("pin-input").focus();
};
$("pin-close").onclick = () => { pinCb = null; $("pin-overlay").classList.add("hidden"); };

// ---------------------------------------------------------------- control panel
// Bảng điều khiển kiểu remote: nút tròn, chức năng chính to ở trên,
// chức năng khác nhỏ hơn và giấu sau nút "⋯ Thêm".
let ctrlTemp = 26;
let ctrlFan = "";    // mức quạt đang chọn (điều hòa)
let ctrlMode = "cool";

// icon rỗng = chỉ chữ, không hiện icon
const BTN_ICON = {
  swing: ["", "Xoay"], fan_mode: ["", "Tốc độ"],
  volume_up: ["🔊", "To hơn"], volume_down: ["🔉", "Nhỏ hơn"], mute: ["🔇", "Tắt tiếng"],
};
const FANS = [["quiet", "Yên lặng"], ["low", "Thấp"], ["mid", "Vừa"], ["high", "Cao"], ["auto", "Tự động"]];
const MODES = [["cool", "Lạnh"], ["dry", "Khô"], ["fan_only", "Gió"], ["heat_cool", "Ổn"]];

function ctrlBody(e) {
  let main = "";
  let more = "";
  if (e.type === "climate") {
    ctrlTemp = (e.defaults && e.defaults.temp) || (e.state && e.state.temperature) || 26;
    ctrlFan = (e.defaults && e.defaults.fan) || "";
    ctrlMode = "cool";
    // hàng 1: bật/tắt riêng; hàng 2: − nhiệt độ ＋
    main = `
      <div class="rrow">
        <button class="rbtn on-btn" data-cmd="on">Bật</button>
        <button class="rbtn off-btn" data-cmd="off">Tắt</button>
      </div>
      <div class="rrow">
        <button class="rbtn sm" data-cmd="temp-">−</button>
        <div class="temp-dial"><div class="dial-temp">❄ <b id="ctrl-temp">${ctrlTemp}</b>°C</div></div>
        <button class="rbtn sm" data-cmd="temp+">＋</button>
      </div>`;
    more = `
      <div class="ctrl-lab">Mức quạt</div>
      <div class="rrow wrap">
        ${FANS.map(([v, lab]) => `<button class="rbtn xs${v === ctrlFan ? " active" : ""}" data-fan="${v}">${lab}</button>`).join("")}
      </div>
      <div class="ctrl-lab">Chế độ</div>
      <div class="rrow wrap">
        ${MODES.map(([v, lab]) => `<button class="rbtn xs${v === "cool" ? " active" : ""}" data-mode="${v}">${lab}</button>`).join("")}
      </div>`;
  } else {
    main = `
      <div class="rrow">
        <button class="rbtn on-btn" data-cmd="on">Bật</button>
        <button class="rbtn off-btn" data-cmd="off">Tắt</button>
      </div>`;
    const extras = e.buttons
      .filter((b) => BTN_ICON[b])
      .map((b) => {
        const [ic, lab] = BTN_ICON[b];
        return `<button class="rbtn xs" data-btn="${b}">${ic ? `<span>${ic}</span>` : ""}${lab}</button>`;
      })
      .join("");
    if (extras) more = `<div class="ctrl-lab">Nút khác</div><div class="rrow wrap">${extras}</div>`;
  }
  return main + (more ? `<button class="more-toggle" id="ctrl-more-btn">⋯ Thêm</button><div id="ctrl-more" class="hidden">${more}</div>` : "");
}

function openCtrl(id) {
  const { e, r } = findEntity(id);
  if (!e) return;
  if (!unlocked()) { showPin(() => openCtrl(id), e.name); return; }
  $("ctrl-icon").textContent = TYPE_ICON[e.type] || "🔘";
  $("ctrl-name").textContent = e.name;
  $("ctrl-room").textContent = r ? r.name : "";
  $("ctrl-body").innerHTML = ctrlBody(e);
  wireCtrl(e);
  $("ctrl-overlay").classList.remove("hidden");
}

function wireCtrl(e) {
  const body = $("ctrl-body");
  body.querySelectorAll("[data-cmd]").forEach((b) =>
    b.addEventListener("click", () => doCommand(e, b.dataset.cmd)));
  body.querySelectorAll("[data-btn]").forEach((b) =>
    b.addEventListener("click", () => doCommand(e, "btn:" + b.dataset.btn)));
  body.querySelectorAll("[data-fan]").forEach((b) =>
    b.addEventListener("click", () => {
      ctrlFan = b.dataset.fan;
      body.querySelectorAll("[data-fan]").forEach((x) => x.classList.toggle("active", x === b));
      sendCommand(e, "set", { mode: ctrlMode, temperature: ctrlTemp, fan: ctrlFan });
    }));
  body.querySelectorAll("[data-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      ctrlMode = b.dataset.mode;
      body.querySelectorAll("[data-mode]").forEach((x) => x.classList.toggle("active", x === b));
      const params = { mode: ctrlMode, temperature: ctrlTemp };
      if (ctrlFan) params.fan = ctrlFan;
      sendCommand(e, "set", params);
    }));
  const moreBtn = body.querySelector("#ctrl-more-btn");
  if (moreBtn) moreBtn.onclick = () => {
    const box = body.querySelector("#ctrl-more");
    box.classList.toggle("hidden");
    moreBtn.textContent = box.classList.contains("hidden") ? "⋯ Thêm" : "⋯ Bớt";
  };
}

$("ctrl-close").onclick = () => $("ctrl-overlay").classList.add("hidden");

async function sendCommand(e, command, params) {
  if (!unlocked()) { $("ctrl-overlay").classList.add("hidden"); showPin(() => openCtrl(e.id), e.name); return; }
  try {
    const resp = await fetch("/api/display/command", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: unlock.token, entity: e.id, command, params }),
    });
    if (resp.status === 401) {
      unlock = { token: null, until: 0 };
      $("ctrl-overlay").classList.add("hidden");
      showPin(null);
      return false;
    }
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      alert(d.detail || "Không điều khiển được");
      return false;
    }
    return true;
  } catch {
    alert("Không kết nối được");
    return false;
  }
}

async function doCommand(e, cmd) {
  if (cmd === "temp-" || cmd === "temp+") {
    ctrlTemp = Math.min(30, Math.max(16, ctrlTemp + (cmd === "temp+" ? 1 : -1)));
    $("ctrl-temp").textContent = ctrlTemp;
    return; // chỉnh số trước, bấm Bật mới gửi
  }
  if (cmd === "on" && e.type === "climate") {
    const params = { mode: ctrlMode, temperature: ctrlTemp };
    if (ctrlFan) params.fan = ctrlFan;
    if (await sendCommand(e, "set", params)) refresh();
    return;
  }
  if (cmd.startsWith("btn:")) {  // nút phụ IR (xoay, volume, timer...)
    if (await sendCommand(e, cmd.slice(4), {})) refresh();
    return;
  }
  if (await sendCommand(e, cmd, {})) refresh();
}

// ---------------------------------------------------------------- timers
function renderTimers(timers) {
  // không có hẹn giờ thì ẩn cả card, khỏi hiện chỗ trống
  const card = $("timers-card");
  card.classList.toggle("hidden", !timers.length);
  if (!timers.length) return;
  $("timer-list").innerHTML = timers.map((t) => {
        const at = new Date(t.target_at);
        const hhmm = at.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", hour12: false });
        const day = at.toLocaleDateString("vi-VN", { day: "numeric", month: "numeric" });
        return `<div class="timer">
          <div class="t-time">${hhmm}<div class="t-room">${day}</div></div>
          <div><div class="t-msg">${esc(t.message)}</div>
          ${t.room ? `<div class="t-room">${esc(t.room)}</div>` : ""}</div>
        </div>`;
      }).join("");
}

refresh();
setInterval(refresh, 30000);
