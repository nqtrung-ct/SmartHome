// SmartHome admin UI - plain JS, no build step.
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = localStorage.getItem("sh_token") || "";
let roomsCache = [];
let eventsTimer = null;

// ---------------------------------------------------------------- api helper

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch("/api" + path, { ...opts, headers });
  if (resp.status === 401 && path !== "/health") {
    showLogin();
    throw new Error("401");
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

function toast(msg, ms = 3500) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

// ---------------------------------------------------------------- login

function showLogin() {
  $("#login").classList.remove("hidden");
}
$("#login-btn").onclick = async () => {
  const value = $("#login-token").value.trim();
  try {
    localStorage.setItem("sh_token", value);
    token = value;
    await api("/rooms");
    $("#login").classList.add("hidden");
    loadTab(currentTab);
  } catch (e) {
    $("#login-error").textContent = "Sai token";
  }
};

// ---------------------------------------------------------------- tabs

let currentTab = "dashboard";
$("#tabs").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-tab]");
  if (!btn) return;
  currentTab = btn.dataset.tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".tab").forEach((t) => t.classList.add("hidden"));
  $("#tab-" + currentTab).classList.remove("hidden");
  clearInterval(eventsTimer);
  if (currentTab === "events") eventsTimer = setInterval(renderEvents, 5000);
  loadTab(currentTab);
});

function loadTab(tab) {
  ({ dashboard: renderDashboard, devices: renderDevices, discovery: renderDiscovery,
     voice: renderVoice, console: renderConsole, events: renderEvents }[tab] || (() => {}))();
}

// ---------------------------------------------------------------- dashboard

async function renderDashboard() {
  const el = $("#tab-dashboard");
  const [overview] = await Promise.all([api("/overview")]);
  roomsCache = overview.rooms;
  const counts = overview.counts;

  const roomCards = overview.rooms.map((room) => {
    const entities = room.entities.map((e) => {
      const state = safeJson(e.state);
      let control = "";
      if (e.entity_type === "switch" || e.entity_type === "light") {
        const on = state.state === "on";
        control = `<span><button class="small-btn ${on ? "" : "primary"}" onclick="toggleEntity(${e.id}, ${on})">${on ? "Tắt" : "Bật"}</button></span>`;
      } else if (e.entity_type === "climate") {
        const temp = state.temperature || 26;
        control = `<span class="small muted">
            <input type="number" id="temp-${e.id}" value="${temp}" min="16" max="30" style="width:60px">
            <button class="small-btn" onclick="setClimate(${e.id})">Đặt</button>
            <button class="small-btn" onclick="climateOff(${e.id})">Tắt</button>
          </span>`;
      } else if (e.entity_type === "sensor") {
        control = `<span class="small muted">${sensorText(state)}</span>`;
      }
      return `<div class="entity">
          <div><div class="name">${esc(e.name)}</div>
          <div class="sub">${esc(e.device_name)} · ${typeVi(e.entity_type)}</div></div>
          ${control}
        </div>`;
    }).join("") || `<div class="muted small" style="padding:8px 0">Phòng trống</div>`;

    return `<div class="card"><h2>🚪 ${esc(room.name)}</h2>${entities}</div>`;
  }).join("");

  const unassigned = overview.unassigned.length
    ? `<div class="card"><h3>Chưa gán phòng</h3>${overview.unassigned.map((e) =>
        `<div class="entity"><div class="name">${esc(e.name)}</div><span class="small muted">${esc(e.device_name)}</span></div>`).join("")}
       <p class="small muted">Gán phòng trong tab Thiết bị để điều khiển bằng giọng nói.</p></div>`
    : "";

  el.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <span class="badge">${counts.devices} thiết bị</span>
      <span class="badge">${counts.entities} thực thể</span>
      <span class="badge">${counts.voice_clients} máy thoại</span>
      <span class="badge">${counts.timers_pending} hẹn giờ chờ</span>
    </div>
    <div class="row" style="margin-bottom:14px">
      <input id="new-room" placeholder="Tên phòng mới (VD: Phòng ngủ)">
      <input id="new-room-aliases" placeholder="Bí danh (VD: ngủ,bedroom)" style="width:220px">
      <button class="primary" onclick="addRoom()">+ Thêm phòng</button>
      <button onclick="seedDemo()">🌱 Dữ liệu demo</button>
    </div>
    <div class="grid">${roomCards}</div>${unassigned}`;
}

function safeJson(s) { try { return JSON.parse(s || "{}"); } catch { return {}; } }
function typeVi(t) {
  return { switch: "công tắc", light: "đèn", climate: "điều hòa", sensor: "cảm biến", camera: "camera" }[t] || t;
}
function sensorText(state) {
  const bits = [];
  if (state.temperature != null) bits.push(state.temperature + "°C");
  if (state.humidity != null) bits.push("💧 " + state.humidity + "%");
  if (state.battery != null) bits.push("🔋 " + state.battery + "%");
  return bits.join(" · ") || "chưa có số liệu";
}

window.addRoom = async () => {
  const name = $("#new-room").value.trim();
  if (!name) return;
  await api("/rooms", { method: "POST", body: JSON.stringify({ name, aliases: $("#new-room-aliases").value }) });
  renderDashboard();
};

window.seedDemo = async () => {
  await api("/demo", { method: "POST" });
  toast("Đã tạo dữ liệu demo");
  renderDashboard();
};

window.toggleEntity = async (id, on) => {
  try {
    await api(`/entities/${id}/command`, { method: "POST", body: JSON.stringify({ command: on ? "off" : "on" }) });
    renderDashboard();
  } catch (e) { toast("Lỗi: " + e.message); }
};
window.setClimate = async (id) => {
  const temp = parseInt($("#temp-" + id).value, 10);
  try {
    await api(`/entities/${id}/command`, { method: "POST", body: JSON.stringify({ command: "set", params: { mode: "cool", temperature: temp } }) });
    renderDashboard();
  } catch (e) { toast("Lỗi: " + e.message); }
};
window.climateOff = async (id) => {
  await api(`/entities/${id}/command`, { method: "POST", body: JSON.stringify({ command: "off" }) }).catch((e) => toast("Lỗi: " + e.message));
  renderDashboard();
};

// ---------------------------------------------------------------- devices

async function renderDevices() {
  const el = $("#tab-devices");
  const [devices, rooms] = await Promise.all([api("/devices"), api("/rooms")]);
  roomsCache = rooms;
  const roomOptions = (selected) =>
    `<option value="">— chưa gán —</option>` + rooms.map((r) =>
      `<option value="${r.id}" ${r.id === selected ? "selected" : ""}>${esc(r.name)}</option>`).join("");

  el.innerHTML = `
    <div class="card">
      <h2>➕ Thêm thiết bị thủ công</h2>
      <div class="row">
        <input id="dev-name" placeholder="Tên thiết bị" style="width:170px">
        <select id="dev-driver">
          <option value="broadlink">Broadlink (IR)</option>
          <option value="tuya">Tuya WiFi</option>
          <option value="zigbee">Zigbee</option>
          <option value="dummy">Ảo (test)</option>
          <option value="generic">Khác</option>
        </select>
        <input id="dev-host" placeholder="IP (nếu có)" style="width:130px">
        <select id="dev-room">${roomOptions(null)}</select>
        <button class="primary" onclick="addDevice()">Thêm</button>
      </div>
      <p class="small muted">Thường thì quét mạng rồi "Thêm vào hệ thống" sẽ dễ hơn.</p>
    </div>
    <div id="device-list"></div>`;

  $("#device-list").innerHTML = devices.map((d) => {
    const entities = d.entities.map((e) => {
      const data = safeJson(e.driver_data);
      const codes = Object.keys(data.codes || {});
      const learnUi = (d.driver === "broadlink")
        ? `<div class="row" style="margin-top:6px">
             <input id="learn-${e.id}" placeholder="Tên nút (off / cool_26 / toggle)" style="width:200px">
             <button class="small-btn" onclick="learnCode(${e.id})">📡 Học mã</button>
             <span id="learn-status-${e.id}" class="small muted"></span>
           </div>
           ${codes.length ? `<div class="row small" style="margin-top:6px">Mã đã học: ${codes.map((c) =>
             `<button class="small-btn" onclick="sendCode(${e.id}, '${esc(c)}')">${esc(c)}</button>`).join(" ")}</div>` : ""}`
        : "";
      return `<div style="border-top:1px solid var(--border); padding:10px 0">
          <div class="row" style="justify-content:space-between">
            <div><b>${esc(e.name)}</b> <span class="badge">${typeVi(e.entity_type)}</span>
              <span class="small muted">${esc(e.aliases)}</span></div>
            <div class="row">
              ${(e.entity_type === "switch" || e.entity_type === "light") ?
                `<button class="small-btn" onclick="cmdEntity(${e.id},'on')">Bật</button>
                 <button class="small-btn" onclick="cmdEntity(${e.id},'off')">Tắt</button>` : ""}
              <button class="small-btn danger" onclick="delEntity(${e.id})">✕</button>
            </div>
          </div>${learnUi}
        </div>`;
    }).join("") || `<p class="small muted">Chưa có thực thể nào.</p>`;

    return `<div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">${esc(d.name)} <span class="badge">${esc(d.driver)}</span>
          ${d.host ? `<span class="small muted">${esc(d.host)}</span>` : ""}</h2>
        <div class="row">
          <select onchange="setDeviceRoom(${d.id}, this.value)">${roomOptions(d.room_id)}</select>
          <button class="small-btn danger" onclick="delDevice(${d.id})">Xóa</button>
        </div>
      </div>
      ${entities}
      <div class="row" style="margin-top:10px">
        <input id="ent-name-${d.id}" placeholder="Tên thực thể (VD: Điều hòa)" style="width:180px">
        <select id="ent-type-${d.id}">
          <option value="switch">công tắc</option><option value="light">đèn</option>
          <option value="climate">điều hòa</option><option value="sensor">cảm biến</option>
        </select>
        <input id="ent-alias-${d.id}" placeholder="Bí danh (VD: điều hòa, máy lạnh)" style="width:200px">
        <button class="small-btn" onclick="addEntity(${d.id})">+ Thực thể</button>
      </div>
    </div>`;
  }).join("") || `<div class="card muted">Chưa có thiết bị nào. Quét mạng ở tab "Quét mạng" hoặc thêm dữ liệu demo ở tab Tổng quan.</div>`;
}

window.addDevice = async () => {
  const body = {
    name: $("#dev-name").value.trim(), driver: $("#dev-driver").value,
    host: $("#dev-host").value.trim(), room_id: +$("#dev-room").value || null,
  };
  if (!body.name) return;
  try { await api("/devices", { method: "POST", body: JSON.stringify(body) }); renderDevices(); }
  catch (e) { toast("Lỗi: " + e.message); }
};
window.setDeviceRoom = async (id, roomId) => {
  await api(`/devices/${id}`, { method: "PATCH", body: JSON.stringify({ room_id: +roomId || null }) });
};
window.delDevice = async (id) => {
  if (!confirm("Xóa thiết bị này?")) return;
  await api(`/devices/${id}`, { method: "DELETE" }); renderDevices();
};
window.addEntity = async (deviceId) => {
  const body = {
    name: $(`#ent-name-${deviceId}`).value.trim(),
    entity_type: $(`#ent-type-${deviceId}`).value,
    aliases: $(`#ent-alias-${deviceId}`).value.trim(),
  };
  if (!body.name) return;
  await api(`/devices/${deviceId}/entities`, { method: "POST", body: JSON.stringify(body) }).catch((e) => toast(e.message));
  renderDevices();
};
window.delEntity = async (id) => {
  await api(`/entities/${id}`, { method: "DELETE" }); renderDevices();
};
window.cmdEntity = async (id, command) => {
  try { await api(`/entities/${id}/command`, { method: "POST", body: JSON.stringify({ command }) }); }
  catch (e) { toast("Lỗi: " + e.message); }
};

window.learnCode = async (entityId) => {
  const button = $(`#learn-${entityId}`).value.trim();
  if (!button) { toast("Nhập tên nút trước"); return; }
  const status = $(`#learn-status-${entityId}`);
  try {
    await api(`/entities/${entityId}/learn`, { method: "POST", body: JSON.stringify({ button }) });
  } catch (e) { status.textContent = "Lỗi: " + e.message; return; }
  const poll = setInterval(async () => {
    try {
      const job = await api(`/entities/${entityId}/learn`);
      if (job.status === "learning") { status.textContent = "Đang chờ bấm remote..."; return; }
      clearInterval(poll);
      status.textContent = job.status === "done" ? "✅ Đã lưu" : (job.status === "timeout" ? "⏰ Hết giờ" : "❌ " + (job.error || job.status));
      setTimeout(renderDevices, 800);
    } catch { clearInterval(poll); }
  }, 1500);
};
window.sendCode = async (entityId, codeName) => {
  // IR codes: switch-style keys send directly, "<mode>_<temp>" parses out mode+temp
  const m = codeName.match(/^([a-z]+)_(\d{2})$/);
  const body = m
    ? { command: "set", params: { mode: m[1], temperature: +m[2] } }
    : { command: codeName, params: {} };
  try { await api(`/entities/${entityId}/command`, { method: "POST", body: JSON.stringify(body) }); toast("Đã gửi: " + codeName); }
  catch (e) { toast("Lỗi: " + e.message); }
};

// ---------------------------------------------------------------- discovery

async function renderDiscovery() {
  const el = $("#tab-discovery");
  el.innerHTML = `
    <div class="card">
      <h2>🔎 Quét mạng</h2>
      <div class="row">
        <button class="primary" id="scan-btn" onclick="startScan()">Bắt đầu quét</button>
        <span id="scan-status" class="muted small"></span>
      </div>
      <p class="small muted">Quét Broadlink, Tuya, mDNS, SSDP, camera ONVIF, Zigbee và ping toàn bộ mạng. Mất ~20-40 giây.</p>
    </div>
    <div id="discovery-list"></div>`;
  refreshDiscovered();
}

async function refreshDiscovered() {
  const list = $("#discovery-list");
  if (!list) return;
  const rows = await api("/discovery").catch(() => []);
  const rooms = await api("/rooms").catch(() => []);
  const roomOptions = `<option value="">— chưa gán phòng —</option>` + rooms.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("");

  list.innerHTML = rows.length ? `
    <div class="card"><table>
      <tr><th>Thiết bị</th><th>IP / ID</th><th>Nguồn</th><th>Hãng</th><th></th></tr>
      ${rows.map((r) => `
        <tr>
          <td><b>${esc(r.name || "?")}</b>
            ${r.supported ? `<span class="badge supported">hỗ trợ</span>` : `<span class="badge">chưa hỗ trợ</span>`}
            ${r.adopted_device_id ? `<span class="badge on">đã thêm</span>` : ""}
            <div class="small muted">${esc(r.method)}${r.mac ? " · " + esc(r.mac) : ""}</div></td>
          <td class="small">${esc(r.ip || r.ident)}</td>
          <td class="small">${esc(r.method)}</td>
          <td class="small">${esc(r.brand || "?")}</td>
          <td>
            ${!r.adopted_device_id ? `
            <div class="row">
              <input id="adopt-name-${r.id}" placeholder="Tên" value="${esc(r.name || "")}" style="width:140px">
              <select id="adopt-room-${r.id}">${roomOptions}</select>
              ${r.method === "tuya" ? `<input id="adopt-key-${r.id}" placeholder="local key" style="width:130px">` : ""}
              ${r.method === "wsdiscovery" ? `<input id="adopt-pass-${r.id}" placeholder="mật khẩu RTSP" style="width:130px">` : ""}
              <button class="small-btn primary" onclick="adopt(${r.id})">Thêm vào hệ thống</button>
              ${!r.supported ? `<button class="small-btn" onclick="adopt(${r.id}, true)">Thêm tay</button>` : ""}
            </div>` : `<span class="small muted">ID thiết bị #${r.adopted_device_id}</span>`}
          </td>
        </tr>`).join("")}
    </table></div>` : `<div class="card muted">Chưa quét gì. Bấm "Bắt đầu quét".</div>`;
}

window.startScan = async () => {
  const btn = $("#scan-btn"), status = $("#scan-status");
  btn.disabled = true;
  await api("/discovery/scan", { method: "POST" }).catch((e) => toast(e.message));
  const poll = setInterval(async () => {
    const job = await api("/discovery/scan").catch(() => null);
    if (!job) { clearInterval(poll); return; }
    status.textContent = job.running ? `Đang quét... (${job.methods.join(", ") || "bắt đầu"})` : "Hoàn tất ✅";
    if (!job.running) { clearInterval(poll); btn.disabled = false; refreshDiscovered(); }
  }, 1500);
};

window.adopt = async (id, force = false) => {
  const body = { force, name: $(`#adopt-name-${id}`).value.trim() || null, room_id: +$(`#adopt-room-${id}`).value || null };
  const key = $(`#adopt-key-${id}`); if (key) body.local_key = key.value.trim();
  const pass = $(`#adopt-pass-${id}`); if (pass) body.rtsp_password = pass.value.trim();
  try {
    const res = await api(`/discovery/${id}/adopt`, { method: "POST", body: JSON.stringify(body) });
    toast(res.warning ? "Đã thêm nhưng có cảnh báo: " + res.warning : "Đã thêm thiết bị #" + res.device_id);
    refreshDiscovered();
  } catch (e) { toast("Lỗi: " + e.message); }
};

// ---------------------------------------------------------------- voice clients

async function renderVoice() {
  const el = $("#tab-voice");
  const [clients, rooms] = await Promise.all([api("/voice-clients"), api("/rooms")]);
  const roomOptions = (sel) => `<option value="">— chưa gán —</option>` + rooms.map((r) =>
    `<option value="${r.id}" ${r.id === sel ? "selected" : ""}>${esc(r.name)}</option>`).join("");

  el.innerHTML = `
    <div class="card">
      <h2>📱 Ghép nối điện thoại</h2>
      <p class="small muted">1. Tạo mã → 2. Mở app trên điện thoại, nhập địa chỉ server + mã → 3. Gán máy vào phòng bên dưới.</p>
      <div class="row">
        <button class="primary" onclick="newPairingCode()">Tạo mã ghép nối</button>
        <span id="pairing-code" class="pairing-code"></span>
        <span class="small muted">Mã dùng 1 lần, hết hạn sau 15 phút.</span>
      </div>
    </div>
    <div class="card">
      <h2>Máy đã ghép nối</h2>
      ${clients.length ? `<table>
        <tr><th>Tên</th><th>Phòng</th><th>Trạng thái</th><th>Lần cuối</th><th></th></tr>
        ${clients.map((c) => `
          <tr>
            <td><b>${esc(c.name)}</b></td>
            <td><select onchange="setClientRoom(${c.id}, this.value)">${roomOptions(c.room_id)}</select></td>
            <td>${c.online ? '<span class="badge on">đang kết nối</span>' : '<span class="badge">offline</span>'}</td>
            <td class="small muted">${esc(c.last_seen || "-")}</td>
            <td><button class="small-btn danger" onclick="delClient(${c.id})">✕</button></td>
          </tr>`).join("")}
      </table>` : `<p class="muted">Chưa có máy nào.</p>`}
    </div>`;
}

window.newPairingCode = async () => {
  const res = await api("/pairing-codes", { method: "POST" });
  $("#pairing-code").textContent = res.code;
};
window.setClientRoom = async (id, roomId) => {
  await api(`/voice-clients/${id}`, { method: "PATCH", body: JSON.stringify({ room_id: +roomId || null }) });
};
window.delClient = async (id) => {
  if (!confirm("Xóa máy này?")) return;
  await api(`/voice-clients/${id}`, { method: "DELETE" }); renderVoice();
};

// ---------------------------------------------------------------- console

async function renderConsole() {
  const el = $("#tab-console");
  const rooms = await api("/rooms");
  const timers = await api("/timers").catch(() => ({ pending: [], recent: [] }));
  el.innerHTML = `
    <div class="card">
      <h2>⌨️ Lệnh văn bản</h2>
      <p class="small muted">Thử như đang nói trong một phòng: "bật đèn", "tắt điều hòa phòng khách", "điều hòa 26 độ", "15 phút nữa nhắc tôi tắt bếp", "thời tiết hôm nay thế nào".</p>
      <div class="row">
        <select id="cmd-room">${`<option value="">— không phòng —</option>` + rooms.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("")}</select>
        <input id="cmd-text" placeholder="Nhập lệnh..." style="flex:1; min-width:220px" onkeydown="if(event.key==='Enter')sendCommand()">
        <button class="primary" onclick="sendCommand()">Gửi</button>
      </div>
      <div id="cmd-out" class="console-out muted">Kết quả sẽ hiện ở đây.</div>
    </div>
    <div class="card">
      <h2>⏰ Hẹn giờ (${timers.pending.length} chờ)</h2>
      ${timers.pending.length ? `<table><tr><th>Lúc</th><th>Nhắc</th><th>Phòng</th><th></th></tr>
        ${timers.pending.map((t) => `<tr><td>${esc(t.target_at)}</td><td>${esc(t.message)}</td>
          <td class="small">${esc(t.room_name || "-")}</td>
          <td><button class="small-btn danger" onclick="cancelTimer(${t.id})">Hủy</button></td></tr>`).join("")}
      </table>` : `<p class="muted small">Không có hẹn giờ nào.</p>`}
      ${timers.recent.length ? `<h3>Gần đây</h3><div class="small muted">${timers.recent.map((t) =>
        `${esc(t.target_at)} · ${esc(t.message)}`).join("<br>")}</div>` : ""}
    </div>`;
}

window.sendCommand = async () => {
  const text = $("#cmd-text").value.trim();
  if (!text) return;
  const out = $("#cmd-out");
  out.classList.remove("muted");
  out.textContent = "…";
  try {
    const res = await api("/command", {
      method: "POST",
      body: JSON.stringify({ text, room_id: +$("#cmd-room").value || null }),
    });
    out.textContent = `Đường dẫn: ${res.engine}\nTrả lời: ${res.reply}` +
      (res.actions?.length ? `\nHành động: ${res.actions.map((a) => JSON.stringify(a)).join(" | ")}` : "");
    setTimeout(renderConsole, 4000);
  } catch (e) { out.textContent = "Lỗi: " + e.message; }
};
window.cancelTimer = async (id) => {
  await api(`/timers/${id}`, { method: "DELETE" }); renderConsole();
};

// ---------------------------------------------------------------- events

async function renderEvents() {
  const el = $("#tab-events");
  const rows = await api("/events?limit=200").catch(() => []);
  el.innerHTML = `<div class="card"><h2>📜 Nhật ký</h2>
    ${rows.length ? `<table><tr><th>Thời gian</th><th>Nguồn</th><th>Nội dung</th></tr>
      ${rows.map((r) => `<tr><td class="small muted" style="white-space:nowrap">${esc(r.ts)}</td>
        <td><span class="badge ${r.level === "warn" ? "off" : ""}">${esc(r.source)}</span></td>
        <td>${esc(r.message)}</td></tr>`).join("")}</table>` : "<p class='muted'>Trống.</p>"}
  </div>`;
}

// ---------------------------------------------------------------- boot

(async () => {
  try {
    await api("/rooms");
    renderDashboard();
  } catch {
    if (token) $("#login-error").textContent = "Token không hợp lệ";
    showLogin();
  }
})();
