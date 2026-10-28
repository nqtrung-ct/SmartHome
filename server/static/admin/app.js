// SmartHome admin UI - plain JS, no build step.
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let token = localStorage.getItem("sh_token") || "";
let eventsTimer = null;
let floorsCache = [];
let devicesCache = [];
let roomsCache = [];

async function loadFloors() {
  floorsCache = await api("/floors").catch(() => []);
  return floorsCache;
}
const floorNames = () => floorsCache.map((f) => f.name);
const floorOptions = (selected) =>
  `<option value="">— chọn tầng —</option>` +
  floorNames().map((f) => `<option value="${esc(f)}" ${f === selected ? "selected" : ""}>${esc(f)}</option>`).join("");
const roomLabel = (r) => (r.floor ? `${r.floor} · ${r.name}` : r.name);

function roomSelectOptions(rooms, selected) {
  return `<option value="">— chưa gán —</option>` + rooms.map((r) =>
    `<option value="${r.id}" ${r.id === selected ? "selected" : ""}>${esc(roomLabel(r))}</option>`).join("");
}

// phòng riêng của thiết bị; rỗng = kế thừa phòng thiết bị gốc
function entityRoomOptions(rooms, own) {
  return `<option value="">— theo thiết bị gốc —</option>` + rooms.map((r) =>
    `<option value="${r.id}" ${r.id === own ? "selected" : ""}>${esc(roomLabel(r))}</option>`).join("");
}

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
  ({ dashboard: renderDashboard, rooms: renderRooms, assistant: renderAssistant,
     devices: renderDevices, discovery: renderDiscovery, voice: renderVoice,
     console: renderConsole, events: renderEvents, settings: renderSettings }[tab] || (() => {}))();
}

// ---------------------------------------------------------------- dashboard: room cards by floor

async function renderDashboard() {
  const el = $("#tab-dashboard");
  const [overview, floors] = await Promise.all([api("/overview"), loadFloors()]);

  // only rooms that actually control/read something
  const withDevices = overview.rooms.filter((r) => r.entities.length > 0);

  const groups = new Map();
  for (const room of withDevices) {
    const f = room.floor || "";
    if (!groups.has(f)) groups.set(f, []);
    groups.get(f).push(room);
  }
  // order: known floors first (as configured), then custom floor names, then rooms without floor
  const order = [
    ...floorNames().filter((f) => groups.has(f)),
    ...[...groups.keys()].filter((f) => f && !floorNames().includes(f)).sort(),
    ...(groups.has("") ? [""] : []),
  ];

  const columns = order.map((floor) => {
    const cards = groups.get(floor).map((room) => {
      const entities = room.entities.map((e) => {
        const state = safeJson(e.state);
        let control = "";
        if (e.entity_type === "controller") {
          // always-on IR hub: green dot, no controls
          control = `<span class="small ok">● Đang bật</span>`;
        } else if (e.entity_type === "switch" || e.entity_type === "light") {
          if (e.readonly) {
            control = `<span class="small ${state.state === "on" ? "ok" : "muted"}">${state.state === "on" ? "● Đang bật" : "○ Đang tắt"}</span>`;
          } else {
            const on = state.state === "on";
            control = `<span><button class="small-btn ${on ? "" : "primary"}" onclick="toggleEntity(${e.id}, ${on})">${on ? "Tắt" : "Bật"}</button></span>`;
          }
        } else if (e.entity_type === "climate") {
          const temp = state.temperature || 26;
          control = `<span class="small muted">
              <input type="number" id="temp-${e.id}" value="${temp}" min="16" max="30" style="width:60px">
              <button class="small-btn" onclick="setClimate(${e.id})">Đặt</button>
              <button class="small-btn" onclick="climateOff(${e.id})">Tắt</button>
            </span>`;
        } else if (e.entity_type === "sensor") {
          // values older than an hour are stale - show a dash, not old numbers
          const stale = e.updated_at && (Date.now() - new Date(e.updated_at).getTime() > 3600000);
          control = `<span class="small muted">${stale ? "—" : sensorText(state)}</span>`;
        }
        return `<div class="entity">
            <div><div class="name">${esc(e.name)}</div>
            <div class="sub">${esc(e.device_name)} · ${typeVi(e.entity_type)}</div></div>
            ${control}
          </div>`;
      }).join("");

      return `<div class="card"><h2>🚪 ${esc(room.name)}</h2>${entities}</div>`;
    }).join("");
    const heading = floor ? `🏢 ${esc(floor)}` : "🏢 Chưa xếp tầng";
    return `<div class="floor-col"><h3 class="floor-heading">${heading}</h3>${cards}</div>`;
  }).join("");

  el.innerHTML = columns
    ? `<div class="floor-columns">${columns}</div>`
    : `<div class="card muted">Chưa có phòng nào có thiết bị. Thêm phòng ở tab <b>Phòng &amp; tầng</b>, rồi thêm thiết bị ở tab <b>Quét mạng</b> hoặc <b>Thiết bị</b>.<br><span class="small">Phòng đã tạo nhưng chưa có thiết bị sẽ được ẩn ở trang này.</span></div>`;
}

function safeJson(s) { try { return JSON.parse(s || "{}"); } catch { return {}; } }
function typeVi(t) {
  return {
    switch: "công tắc", light: "đèn", climate: "điều hòa",
    sensor: "cảm biến", camera: "camera", controller: "điều khiển",
  }[t] || t;
}
function sensorText(state) {
  const bits = [];
  if (state.temperature != null) bits.push("🌡 " + state.temperature + "°C");
  if (state.humidity != null) bits.push("💧 " + state.humidity + "%");
  if (state.battery != null) bits.push("🔋 " + state.battery + "%");
  if (state.power != null) bits.push("⚡ " + (state.power >= 1000 ? (state.power / 1000).toFixed(1) + " kW" : state.power.toFixed(0) + " W"));
  if (state.voltage != null) bits.push(state.voltage.toFixed(0) + "V");
  // tổng kWh đã tiêu thụ: không hiện trên UI (chỉ trả lời khi hỏi bằng giọng nói)
  return bits.join(" · ") || "chưa có số liệu";
}

// devices-tab entities carry driver_data; dashboard rows carry a computed flag
function isReadonly(e) {
  return e.readonly === true || safeJson(e.driver_data).readonly === true;
}

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

// ---------------------------------------------------------------- rooms & floors management

async function renderRooms() {
  const el = $("#tab-rooms");
  const [rooms, floors] = await Promise.all([api("/rooms"), loadFloors()]);

  const floorRows = floors.map((f) => `
    <div class="row manage-row">
      <input id="floor-name-${f.id}" value="${esc(f.name)}" style="width:200px">
      <button class="small-btn" onclick="saveFloor(${f.id})">💾</button>
      <button class="small-btn danger" onclick="delFloor(${f.id}, ${esc(JSON.stringify(f.name))})">🗑</button>
    </div>`).join("");

  const roomRows = rooms.map((r) => `
    <div class="row manage-row" style="flex-wrap:wrap">
      <input id="room-name-${r.id}" value="${esc(r.name)}" style="width:150px">
      <select id="room-floor-${r.id}" onchange="saveRoom(${r.id})">${floorOptions(r.floor)}</select>
      <input id="room-alias-${r.id}" value="${esc(r.aliases)}" placeholder="bí danh" style="width:180px">
      <span class="small muted" style="min-width:70px">${r.entity_count} thực thể</span>
      <button class="small-btn" onclick="saveRoom(${r.id})">💾</button>
      <button class="small-btn danger" onclick="delRoom(${r.id}, ${esc(JSON.stringify(r.name))})">🗑</button>
    </div>`).join("");

  el.innerHTML = `
    <div class="card">
      <h2>🏢 Tầng</h2>
      <p class="small muted">Đổi tên tầng sẽ áp dụng cho mọi phòng của tầng đó. Xóa tầng không xóa phòng — phòng chỉ mất tầng.</p>
      <div class="row" style="margin-bottom:10px">
        <input id="new-floor" placeholder="Tên tầng mới (VD: Tầng hầm)" style="width:200px">
        <button class="primary small-btn" onclick="addFloor()">+ Thêm tầng</button>
      </div>
      ${floorRows || `<p class="muted small">Chưa có tầng nào.</p>`}
    </div>
    <div class="card">
      <h2>🚪 Phòng</h2>
      <p class="small muted">Sửa tên / tầng / bí danh rồi bấm 💾. Bí danh giúp nhận ra phòng khi nói ("bật đèn phòng ngủ").</p>
      <div class="row" style="margin-bottom:10px">
        <input id="new-room" placeholder="Tên phòng mới (VD: Phòng ngủ)" style="width:150px">
        <select id="new-room-floor">${floorOptions("")}</select>
        <input id="new-room-aliases" placeholder="Bí danh (VD: ngủ,bedroom)" style="width:180px">
        <button class="primary small-btn" onclick="addRoom()">+ Thêm phòng</button>
      </div>
      ${roomRows || `<p class="muted small">Chưa có phòng nào.</p>`}
    </div>`;
}

window.addFloor = async () => {
  const name = $("#new-floor").value.trim();
  if (!name) return;
  try { await api("/floors", { method: "POST", body: JSON.stringify({ name }) }); renderRooms(); }
  catch (e) { toast("Lỗi: " + e.message); }
};
window.saveFloor = async (id) => {
  const name = $(`#floor-name-${id}`).value.trim();
  if (!name) { toast("Tên tầng không được trống"); return; }
  try { await api(`/floors/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }); toast("Đã lưu tầng"); }
  catch (e) { toast("Lỗi: " + e.message); }
};
window.delFloor = async (id, name) => {
  if (!confirm(`Xóa tầng "${name}"? Các phòng của tầng này sẽ thành "chưa xếp tầng".`)) return;
  await api(`/floors/${id}`, { method: "DELETE" }).catch((e) => toast("Lỗi: " + e.message));
  renderRooms();
};
window.addRoom = async () => {
  const name = $("#new-room").value.trim();
  if (!name) return;
  try {
    await api("/rooms", {
      method: "POST",
      body: JSON.stringify({ name, floor: $("#new-room-floor").value, aliases: $("#new-room-aliases").value }),
    });
    renderRooms();
  } catch (e) { toast("Lỗi: " + e.message); }
};
window.saveRoom = async (id) => {
  const body = {
    name: $(`#room-name-${id}`).value.trim(),
    floor: $(`#room-floor-${id}`).value,
    aliases: $(`#room-alias-${id}`).value.trim(),
  };
  if (!body.name) { toast("Tên phòng không được trống"); return; }
  try { await api(`/rooms/${id}`, { method: "PATCH", body: JSON.stringify(body) }); toast("Đã lưu phòng"); }
  catch (e) { toast("Lỗi: " + e.message); }
};
window.delRoom = async (id, name) => {
  if (!confirm(`Xóa phòng "${name}"? Thiết bị trong phòng sẽ thành "chưa gán phòng".`)) return;
  await api(`/rooms/${id}`, { method: "DELETE" }).catch((e) => toast("Lỗi: " + e.message));
  renderRooms();
};

// ---------------------------------------------------------------- assistant (AI models + voice test)

const dot = (ok, okText, badText) =>
  ok ? `<span class="badge on">${okText}</span>` : `<span class="badge off">${badText}</span>`;

async function renderAssistant() {
  const el = $("#tab-assistant");
  const st = await api("/assistant/status").catch(() => null);
  if (!st) { el.innerHTML = `<div class="card muted">Không tải được trạng thái trợ lý.</div>`; return; }

  // --- LLM ---
  const llmModels = st.llm.models.length
    ? st.llm.models.map((m) => `<option value="${esc(m.name)}" ${m.name === st.llm.model ? "selected" : ""}>${esc(m.name)} (${m.size_gb} GB)</option>`).join("")
    : `<option value="${esc(st.llm.model)}">${esc(st.llm.model)}</option>`;
  el.innerHTML = `
    <div class="card">
      <h2>🧠 Mô hình ngôn ngữ (Ollama) ${dot(st.llm.reachable, "đang chạy", "không kết nối được")}</h2>
      ${st.llm.reachable ? `
        <div class="row">
          <label class="small muted">Model</label>
          <select id="llm-model" style="min-width:220px">${llmModels}</select>
          <button class="primary small-btn" onclick="saveAssistantConfig()">💾 Lưu</button>
          <button class="small-btn" onclick="testLlm()">▶ Thử LLM</button>
        </div>
        <div id="llm-test-out" class="console-out muted small"></div>`
      : `<p class="small">Chưa chạy Ollama. Cài tại <a href="https://ollama.com" target="_blank" rel="noopener">ollama.com</a> rồi:<br>
         <code>ollama pull qwen3:4b</code><br>Lỗi: ${esc(st.llm.error || "?")}</p>`}
    </div>

    <div class="card">
      <h2>🎙 Nhận dạng giọng nói (STT) ${dot(st.stt.loaded, "model đã nạp", "chưa nạp")}</h2>
      ${!st.stt.loaded && st.stt.error ? `<p class="small error">Lý do: ${esc(st.stt.error)}</p>` : ""}
      <div class="row">
        <label class="small muted">Engine</label>
        <select id="stt-engine">
          <option value="none" ${st.stt.engine === "none" ? "selected" : ""}>none (tắt)</option>
          <option value="sherpa" ${st.stt.engine === "sherpa" ? "selected" : ""}>sherpa-onnx Zipformer-vi</option>
        </select>
        <button class="primary small-btn" onclick="saveAssistantConfig()">💾 Lưu</button>
      </div>
      ${!st.stt.loaded ? `<p class="small">Cài đặt: <code>pip install -r optional-requirements.txt</code>, tải model Zipformer-vi (mục Vietnamese) từ
        <a href="https://k2-fsa.github.io/sherpa/onnx/pretrained_models/index.html" target="_blank" rel="noopener">sherpa-onnx</a>,
        giải nén vào <code>data/models/stt/</code> (tokens.txt + encoder/decoder/joiner .onnx), chọn engine sherpa rồi Lưu.</p>` : ""}
    </div>

    <div class="card">
      <h2>🔊 Chuyển giọng nói (TTS) ${dot(st.tts.loaded, "sẵn sàng", "chỉ hiện text")}</h2>
      ${!st.tts.loaded && st.tts.error ? `<p class="small error">Lý do: ${esc(st.tts.error)}</p>` : ""}
      <div class="row">
        <label class="small muted">Engine</label>
        <select id="tts-engine">
          <option value="none" ${st.tts.engine === "none" ? "selected" : ""}>none (tắt)</option>
          <option value="http" ${st.tts.engine === "http" ? "selected" : ""}>http (POST text → wav)</option>
        </select>
        <input id="tts-url" value="${esc(st.tts.http_url)}" placeholder="http://127.0.0.1:8125/tts" style="width:230px">
        <button class="primary small-btn" onclick="saveAssistantConfig()">💾 Lưu</button>
        <button class="small-btn" onclick="testTts()">▶ Phát thử</button>
      </div>
      <div id="tts-test-out" class="console-out muted small"></div>
    </div>`;
}

window.saveAssistantConfig = async () => {
  const body = {
    llm_model: $("#llm-model")?.value || null,
    stt_engine: $("#stt-engine")?.value || null,
    tts_engine: $("#tts-engine")?.value || null,
    tts_http_url: $("#tts-url")?.value ?? null,
  };
  try {
    const res = await api("/assistant/config", { method: "POST", body: JSON.stringify(body) });
    toast(res.changed.length ? "Đã lưu: " + res.changed.join("; ") : "Không có gì thay đổi");
    setTimeout(renderAssistant, 400);
  } catch (e) { toast("Lỗi: " + e.message); }
};

window.testLlm = async () => {
  const out = $("#llm-test-out");
  out.classList.remove("muted");
  out.textContent = "…";
  const t0 = performance.now();
  try {
    const res = await api("/command", {
      method: "POST",
      body: JSON.stringify({ text: "tắt tất cả đèn rồi cho mình biết thời tiết hôm nay" }),
    });
    const ms = Math.round(performance.now() - t0);
    out.textContent = `Đường dẫn: ${res.engine} · ${ms} ms\nTrả lời: ${res.reply}` +
      (res.actions?.length ? `\nHành động: ${res.actions.map((a) => JSON.stringify(a)).join(" | ")}` : "");
  } catch (e) { out.textContent = "Lỗi: " + e.message; }
};

window.testTts = async () => {
  const out = $("#tts-test-out");
  out.classList.remove("muted");
  out.textContent = "Đang tổng hợp…";
  try {
    const resp = await fetch("/api/assistant/test-tts", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify({ text: "Đã tắt điều hòa phòng ngủ." }),
    });
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail; } catch {}
      out.textContent = "❌ " + detail;
      return;
    }
    const ms = resp.headers.get("X-Latency-Ms");
    const rate = resp.headers.get("X-Sample-Rate");
    const blob = await resp.blob();
    new Audio(URL.createObjectURL(blob)).play();
    out.textContent = `✅ Đang phát · tổng hợp ${ms} ms · ${rate} Hz · ${(blob.size / 1024).toFixed(0)} KB`;
  } catch (e) { out.textContent = "Lỗi: " + e.message; }
};

// ---------------------------------------------------------------- devices

async function renderDevices() {
  const el = $("#tab-devices");
  const [devices, rooms] = await Promise.all([api("/devices"), api("/rooms"), loadFloors()]);
  devicesCache = devices;
  roomsCache = rooms;

  if (!devices.length) {
    el.innerHTML = `<div class="card muted">Chưa có thiết bị nào.<br>
      <span class="small">Vào tab <b>Quét mạng</b> để quét và thêm thiết bị, hoặc dùng nút "➕ Thêm tay" ở đó.</span></div>`;
    return;
  }

  // one row per entity ("thiết bị" người dùng thấy), grouped theo phòng hiệu dụng
  // (phòng của thực thể nếu có, không thì theo thiết bị gốc)
  const items = [];
  for (const d of devices) for (const e of d.entities) items.push([e, d]);
  const byRoom = new Map();
  for (const it of items) {
    const rid = it[0].room_id ?? it[1].room_id ?? null;
    if (!byRoom.has(rid)) byRoom.set(rid, []);
    byRoom.get(rid).push(it);
  }

  const entityRow = ([e, d]) => {
    const data = safeJson(e.driver_data);
    const codes = Object.keys(data.codes || {});
    const isHub = e.entity_type === "controller";
    const readonly = isReadonly(e) || isHub;
    const controls = (e.entity_type === "switch" || e.entity_type === "light") && !readonly
      ? `<button class="small-btn" onclick="cmdEntity(${e.id},'on')">Bật</button>
         <button class="small-btn" onclick="cmdEntity(${e.id},'off')">Tắt</button>` : "";
    const learnUi = d.driver === "broadlink"
      ? `<div class="row small" style="padding:6px 0 9px;border-bottom:1px solid var(--border)">
           <input id="learn-${e.id}" placeholder="Tên nút (off / cool_26 / toggle)" style="width:200px">
           <button class="small-btn" onclick="learnCode(${e.id})">📡 Học mã</button>
           <span id="learn-status-${e.id}" class="small muted"></span>
           ${codes.length ? `<span>Mã đã học: ${codes.map((c) =>
             `<button class="small-btn" onclick="sendCode(${e.id}, '${esc(c)}')">${esc(c)}</button>`).join(" ")}</span>` : ""}
         </div>` : "";
    return `<div class="entity">
        <div style="flex:1;min-width:0">
          <div class="name">${esc(e.name)} <span class="badge">${typeVi(e.entity_type)}</span></div>
          <div class="sub">${esc(d.name)}${d.host ? " · " + esc(d.host) : ""}${e.aliases ? " · bí danh: " + esc(e.aliases) : ""}</div>
        </div>
        <div class="row">
          ${controls}
          <button class="small-btn" onclick="openEditModal(${e.id})" title="Sửa tên, phòng, thiết bị con">✏️</button>
          <button class="small-btn danger" onclick="delEntity(${e.id})">✕</button>
        </div>
      </div>${learnUi}`;
  };

  // cột theo tầng như dashboard
  const groups = new Map();
  for (const r of rooms) {
    if (!byRoom.has(r.id)) continue;
    const f = r.floor || "";
    if (!groups.has(f)) groups.set(f, []);
    groups.get(f).push(r);
  }
  const order = [
    ...floorNames().filter((f) => groups.has(f)),
    ...[...groups.keys()].filter((f) => f && !floorNames().includes(f)).sort(),
    ...(groups.has("") ? [""] : []),
  ];
  const columns = order.map((floor) => {
    const cards = groups.get(floor).map((r) =>
      `<div class="card"><h2>🚪 ${esc(r.name)}</h2>${byRoom.get(r.id).map(entityRow).join("")}</div>`
    ).join("");
    const heading = floor ? `🏢 ${esc(floor)}` : "🏢 Chưa xếp tầng";
    return `<div class="floor-col"><h3 class="floor-heading">${heading}</h3>${cards}</div>`;
  }).join("");
  const unassigned = (byRoom.get(null) || []);
  const unassignedCol = unassigned.length
    ? `<div class="floor-col"><h3 class="floor-heading">📦 Chưa gán phòng</h3>
         <div class="card">${unassigned.map(entityRow).join("")}</div></div>` : "";

  el.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <span class="small muted">Nhấn ✏️ để sửa tên, phòng (và thiết bị con với điều khiển). Thêm thiết bị mới ở tab "Quét mạng".</span>
    </div>
    <div class="floor-columns">${columns}${unassignedCol}</div>`;
}

window.delEntity = async (id) => {
  const d = devicesCache.find((x) => x.entities.some((e) => e.id === id));
  const e = d && d.entities.find((x) => x.id === id);
  if (e && e.entity_type === "controller") {
    // xóa hub = xóa cả thiết bị con gắn với nó
    const n = d.entities.length - 1;
    if (!confirm(n ? `Xóa '${e.name}' sẽ xóa luôn ${n} thiết bị con?` : `Xóa '${e.name}'?`)) return;
    await api(`/devices/${d.id}`, { method: "DELETE" });
  } else {
    if (!confirm(`Xóa '${e ? e.name : id}'?`)) return;
    await api(`/entities/${id}`, { method: "DELETE" });
  }
  renderDevices();
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

// ---------------------------------------------------------------- manual device modal

let modalCtx = {}; // {discoveredId?: number}

window.openDeviceModal = async (ctx = {}) => {
  modalCtx = ctx;
  $("#m-title").textContent = ctx.discoveredId ? "➕ Thêm thiết bị (từ kết quả quét)" : "➕ Thêm thiết bị thủ công";
  $("#m-name").value = ctx.name || "";
  $("#m-host").value = ctx.host || "";
  $("#m-driver").value = ctx.driver || "broadlink";
  $("#m-mac").value = "";
  $("#m-tuya-id").value = "";
  $("#m-tuya-key").value = "";
  $("#m-rtsp").value = "";
  $("#m-error").textContent = "";
  $("#m-newroom").checked = false;
  $("#m-newroom-wrap").classList.add("hidden");
  $("#m-newroom-name").value = "";

  const [rooms, floors] = await Promise.all([api("/rooms").catch(() => []), loadFloors()]);
  $("#m-room").innerHTML = roomSelectOptions(rooms, ctx.room_id ?? null);
  $("#m-newroom-floor").innerHTML = floorOptions("");

  $("#device-modal").classList.remove("hidden");
  $("#m-name").focus();
};

function toggleDriverFields() {
  const driver = $("#m-driver").value;
  $$(".driver-field").forEach((el) => el.classList.add("hidden"));
  $$(`.driver-field.df-${driver}`).forEach((el) => el.classList.remove("hidden"));
}
$("#m-driver").addEventListener("change", toggleDriverFields);
$("#m-newroom").addEventListener("change", () =>
  $("#m-newroom-wrap").classList.toggle("hidden", !$("#m-newroom").checked));
$("#m-cancel").onclick = () => $("#device-modal").classList.add("hidden");

$("#m-save").onclick = async () => {
  const err = $("#m-error");
  err.textContent = "";
  const name = $("#m-name").value.trim();
  const driver = $("#m-driver").value;
  const host = $("#m-host").value.trim();
  if (!name) { err.textContent = "Nhập tên thiết bị"; return; }
  if ((driver === "broadlink" || driver === "tuya" || driver === "onvif") && !host) {
    err.textContent = "Thiết bị loại này cần địa chỉ IP"; return;
  }
  try {
    // room: existing or create new (with floor) on the fly
    let roomId = +$("#m-room").value || null;
    if ($("#m-newroom").checked) {
      const roomName = $("#m-newroom-name").value.trim();
      if (!roomName) { err.textContent = "Nhập tên phòng mới"; return; }
      const room = await api("/rooms", {
        method: "POST",
        body: JSON.stringify({ name: roomName, floor: $("#m-newroom-floor").value }),
      });
      roomId = room.id;
    }

    const extra = {};
    if (driver === "tuya") {
      const id = $("#m-tuya-id").value.trim();
      const key = $("#m-tuya-key").value.trim();
      if (!id || !key) { err.textContent = "Tuya cần Device ID và Local key"; return; }
      extra.id = id; extra.key = key; extra.ver = "3.3";
    }
    if (driver === "broadlink" && $("#m-mac").value.trim()) extra.mac = $("#m-mac").value.trim();
    if (driver === "onvif" && $("#m-rtsp").value) extra.rtsp_password = $("#m-rtsp").value;

    const result = await api("/devices", {
      method: "POST",
      body: JSON.stringify({ name, driver, host, mac: extra.mac || "", room_id: roomId, extra }),
    });
    if (modalCtx.discoveredId) {
      await api(`/discovery/${modalCtx.discoveredId}`, {
        method: "PATCH",
        body: JSON.stringify({ adopted_device_id: result.id }),
      }).catch(() => {});
    }
    $("#device-modal").classList.add("hidden");
    toast(`Đã thêm "${name}"` + (result.entities.length ? ` (+${result.entities.length} thực thể)` : ""));
    loadTab(currentTab);
  } catch (e) {
    err.textContent = "Lỗi: " + e.message;
  }
};

// ---------------------------------------------------------------- edit device modal

let editCtx = null; // {e, d, removed: Set<childId>}

function childRow(c) {
  const type = c ? c.entity_type : "climate";
  const opt = (v, label) => `<option value="${v}" ${type === v ? "selected" : ""}>${label}</option>`;
  return `<div class="row ec-row" data-id="${c ? c.id : ""}" style="margin-bottom:6px">
    <input class="ec-name" placeholder="Tên (VD: Điều hòa)" style="width:150px" value="${esc(c ? c.name : "")}">
    <select class="ec-type">${opt("switch", "công tắc")}${opt("light", "đèn")}${opt("climate", "điều hòa")}${opt("sensor", "cảm biến")}</select>
    <input class="ec-alias" placeholder="Bí danh (máy lạnh...)" style="width:150px" value="${esc(c ? c.aliases : "")}">
    <button class="small-btn danger" onclick="removeChildRow(this)" title="Xóa thiết bị con">✕</button>
  </div>`;
}

window.openEditModal = (entityId) => {
  const d = devicesCache.find((x) => x.entities.some((e) => e.id === entityId));
  const e = d && d.entities.find((x) => x.id === entityId);
  if (!e || !d) return;
  const isHub = e.entity_type === "controller";
  editCtx = { e, d, removed: new Set() };
  $("#e-title").textContent = isHub ? "✏️ Sửa điều khiển" : "✏️ Sửa thiết bị";
  $("#e-name").value = e.name;
  $("#e-alias").value = e.aliases || "";
  $("#e-hw").textContent = `Thiết bị gốc: ${d.name}${d.host ? " · " + d.host : ""}`;
  // hiện phòng hiệu dụng (phòng riêng nếu có, không thì phòng thiết bị gốc);
  // chọn "— theo thiết bị gốc —" để quay lại kế thừa
  const effRoom = e.room_id ?? d.room_id ?? null;
  $("#e-room").innerHTML = isHub
    ? roomSelectOptions(roomsCache, d.room_id)
    : entityRoomOptions(roomsCache, effRoom);
  editCtx.roomInit = +$("#e-room").value || null;
  $("#e-children-wrap").classList.toggle("hidden", !isHub);
  if (isHub) {
    const children = d.entities.filter((x) => x.id !== e.id);
    $("#e-children").innerHTML = children.map(childRow).join("")
      || `<p class="small muted">Chưa có thiết bị con. Thêm rồi học mã IR ngay trên dòng của nó ở tab Thiết bị.</p>`;
  }
  $("#edit-modal").classList.remove("hidden");
  $("#e-name").focus();
};

window.removeChildRow = (btn) => {
  const row = btn.parentElement;
  if (row.dataset.id) editCtx.removed.add(+row.dataset.id);
  row.remove();
};

$("#e-add-child").onclick = () => {
  const empty = $("#e-children .small.muted");
  if (empty) empty.remove();
  $("#e-children").insertAdjacentHTML("beforeend", childRow(null));
};
$("#e-cancel").onclick = () => $("#edit-modal").classList.add("hidden");

$("#e-save").onclick = async () => {
  const { e, d, removed, roomInit } = editCtx;
  const isHub = e.entity_type === "controller";
  const name = $("#e-name").value.trim();
  const aliases = $("#e-alias").value.trim();
  const roomId = +$("#e-room").value || null;
  const roomChanged = roomId !== roomInit;  // không đụng phòng = giữ nguyên kế thừa
  if (!name) { toast("Nhập tên thiết bị"); return; }
  try {
    await api(`/entities/${e.id}`, { method: "PATCH", body: JSON.stringify({ name, aliases }) });
    if (isHub) {
      if (roomChanged) {
        await api(`/devices/${d.id}`, { method: "PATCH", body: JSON.stringify({ room_id: roomId }) });
      }
      for (const id of removed) await api(`/entities/${id}`, { method: "DELETE" });
      for (const row of $$("#e-children .ec-row")) {
        const child = {
          name: row.querySelector(".ec-name").value.trim(),
          entity_type: row.querySelector(".ec-type").value,
          aliases: row.querySelector(".ec-alias").value.trim(),
        };
        if (!child.name) continue;
        if (row.dataset.id) {
          await api(`/entities/${row.dataset.id}`, { method: "PATCH", body: JSON.stringify(child) });
        } else {
          await api(`/devices/${d.id}/entities`, { method: "POST", body: JSON.stringify(child) });
        }
      }
    } else if (roomChanged) {
      await api(`/entities/${e.id}`, { method: "PATCH", body: JSON.stringify({ room_id: roomId }) });
    }
    $("#edit-modal").classList.add("hidden");
    renderDevices();
    toast("Đã lưu");
  } catch (err) {
    toast("Lỗi: " + err.message);
  }
};

$("#e-delete").onclick = async () => {
  const { e, d } = editCtx;
  const isHub = e.entity_type === "controller";
  const n = isHub ? d.entities.length - 1 : 0;
  const msg = isHub && n ? `Xóa '${e.name}' sẽ xóa luôn ${n} thiết bị con?` : `Xóa '${e.name}'?`;
  if (!confirm(msg)) return;
  // hub hoặc thiết bị cuối cùng của phần cứng: xóa cả phần cứng
  if (isHub || d.entities.length === 1) await api(`/devices/${d.id}`, { method: "DELETE" });
  else await api(`/entities/${e.id}`, { method: "DELETE" });
  $("#edit-modal").classList.add("hidden");
  renderDevices();
};

// ---------------------------------------------------------------- discovery

async function renderDiscovery() {
  const el = $("#tab-discovery");
  el.innerHTML = `
    <div class="card">
      <h2>🔎 Quét mạng</h2>
      <div class="row">
        <button class="primary" id="scan-btn" onclick="startScan()">Bắt đầu quét</button>
        <button id="manual-btn" onclick="openDeviceModal()">➕ Thêm tay</button>
        <span id="scan-status" class="muted small"></span>
      </div>
      <p class="small muted">Quét Broadlink, Tuya, mDNS, SSDP, camera ONVIF, Zigbee và ping toàn bộ mạng. Mất ~20-40 giây.<br>
      "Thêm tay" dùng khi quét không thấy: nhập IP, loại thiết bị, tầng/phòng và các thông số khác.</p>
    </div>
    <div id="discovery-list"></div>`;
  refreshDiscovered();
}

async function refreshDiscovered() {
  const list = $("#discovery-list");
  if (!list) return;
  const rows = await api("/discovery").catch(() => []);
  const rooms = await api("/rooms").catch(() => []);

  list.innerHTML = rows.length ? `
    <div class="card"><table>
      <tr><th>Thiết bị</th><th>IP / ID</th><th>Nguồn</th><th>Hãng</th><th></th></tr>
      ${rows.map((r) => {
        const guessedDriver = { broadlink: "broadlink", tuya: "tuya", wsdiscovery: "onvif" }[r.method] || "generic";
        return `
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
              <input id="adopt-name-${r.id}" placeholder="Tên" value="${esc(r.name || "")}" style="width:130px">
              <select id="adopt-room-${r.id}">${roomSelectOptions(rooms, null)}</select>
              ${r.method === "tuya" ? `<input id="adopt-key-${r.id}" placeholder="🔑 Local key" style="width:110px">` : ""}
              <button class="small-btn primary" onclick="adopt(${r.id})">Thêm vào hệ thống</button>
              <button class="small-btn" onclick='openDeviceModal({discoveredId:${r.id}, name:${esc(JSON.stringify(r.name || ""))}, host:${esc(JSON.stringify(r.ip || ""))}, driver:"${guessedDriver}"})'>⚙️ Nhập tay</button>
            </div>` : `<span class="small muted">Thiết bị #${r.adopted_device_id}</span>`}
          </td>
        </tr>`;
      }).join("")}
    </table></div>` : `<div class="card muted">Chưa quét gì. Bấm "Bắt đầu quét", hoặc "Thêm tay" nếu biết IP thiết bị.</div>`;
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

window.adopt = async (id) => {
  const body = {
    name: $(`#adopt-name-${id}`).value.trim() || null,
    room_id: +$(`#adopt-room-${id}`).value || null,
  };
  const keyEl = $(`#adopt-key-${id}`);
  if (keyEl && keyEl.value.trim()) body.local_key = keyEl.value.trim();
  try {
    const res = await api(`/discovery/${id}/adopt`, { method: "POST", body: JSON.stringify(body) });
    toast(res.warning ? "Đã thêm nhưng có cảnh báo: " + res.warning : "Đã thêm thiết bị #" + res.device_id);
    refreshDiscovered();
  } catch (e) { toast("Lỗi: " + e.message + " — thử nút ⚙️ Nhập tay"); }
};

// ---------------------------------------------------------------- voice clients

async function renderVoice() {
  const el = $("#tab-voice");
  const [clients, rooms] = await Promise.all([api("/voice-clients"), api("/rooms")]);

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
            <td><select onchange="setClientRoom(${c.id}, this.value)">${roomSelectOptions(rooms, c.room_id)}</select></td>
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
        <select id="cmd-room">${`<option value="">— không phòng —</option>` + rooms.map((r) =>
          `<option value="${r.id}">${esc(roomLabel(r))}</option>`).join("")}</select>
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

let eventsDate = "";  // YYYY-MM-DD đang xem; rỗng = hôm nay

const localDateStr = (d = new Date()) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

window.setEventsDate = (delta) => {
  const [y, m, d] = eventsDate.split("-").map(Number);
  eventsDate = localDateStr(new Date(y, m - 1, d + delta));
  document.activeElement?.blur();
  renderEvents(true);
};

window.onEventsDatePick = (value) => {
  eventsDate = value;
  document.activeElement?.blur();
  renderEvents(true);
};

async function renderEvents(force = false) {
  const el = $("#tab-events");
  // đang chọn ngày trên calendar thì đừng vẽ lại (sẽ đóng popup + mất focus)
  if (!force && el.contains(document.activeElement)) return;
  if (!eventsDate) eventsDate = localDateStr();
  const rows = await api(`/events?date=${eventsDate}`).catch(() => []);
  const today = localDateStr();
  el.innerHTML = `<div class="card"><h2>📜 Nhật ký ${eventsDate === today ? "(hôm nay)" : ""}</h2>
    <div class="row" style="margin-bottom:10px">
      <button class="small-btn" onclick="setEventsDate(-1)">◀</button>
      <input type="date" value="${eventsDate}" onchange="onEventsDatePick(this.value)" style="width:170px">
      <button class="small-btn" onclick="setEventsDate(1)">▶</button>
      ${eventsDate !== today ? `<button class="small-btn" onclick="onEventsDatePick('${today}')">Hôm nay</button>` : ""}
      <span class="small muted">${rows.length} dòng</span>
    </div>
    ${rows.length ? `<table><tr><th>Giờ</th><th>Nguồn</th><th>Nội dung</th></tr>
      ${rows.map((r) => `<tr><td class="small muted" style="white-space:nowrap">${esc(r.ts.slice(11, 19))}</td>
        <td><span class="badge ${r.level === "warn" ? "off" : ""}">${esc(r.source)}</span></td>
        <td>${esc(r.message)}</td></tr>`).join("")}</table>` : "<p class='muted'>Không có gì trong ngày này.</p>"}
  </div>`;
}

// ---------------------------------------------------------------- settings

const toggle = (id, checked, label) => `
  <label class="setting-toggle">
    <input type="checkbox" id="${id}" ${checked ? "checked" : ""}>
    <span>${label}</span>
  </label>`;

async function renderSettings() {
  const el = $("#tab-settings");
  const st = await api("/settings").catch(() => null);
  if (!st) { el.innerHTML = `<div class="card muted">Không tải được cài đặt.</div>`; return; }

  el.innerHTML = `
    <div class="card">
      <h2>📡 Kết nối Zigbee (MQTT)</h2>
      ${toggle("s-mqtt", st.mqtt.enabled, "Kết nối Mosquitto / Zigbee2MQTT")}
      <p class="small muted">Chỉ bật khi đã cài Mosquitto + Zigbee2MQTT (xem docs/setup-windows.md §4).
      Tắt sẽ ngừng thử kết nối và ngừng nhận sensor Zigbee.</p>
      <div class="row">
        <label class="small muted">Broker</label><input id="s-mqtt-host" value="${esc(st.mqtt.host)}" style="width:140px">
        <label class="small muted">Port</label><input id="s-mqtt-port" value="${st.mqtt.port}" style="width:80px">
        <label class="small muted">Topic</label><input id="s-mqtt-topic" value="${esc(st.mqtt.base_topic)}" style="width:150px">
      </div>
    </div>
    <div class="card">
      <h2>🔎 Quét mạng</h2>
      ${toggle("s-scan", st.discovery.network_scan, "Ping toàn bộ mạng khi quét (tìm thiết bị chưa biết)")}
      <div class="row">
        <label class="small muted">Dải mạng</label>
        <input id="s-subnet" value="${esc(st.discovery.subnet)}" placeholder="auto hoặc 192.168.1.0/24" style="width:200px">
      </div>
      <p class="small muted">"auto" = tự dò theo card mạng mặc định. Tắt ping sẽ quét nhanh hơn nhưng chỉ thấy thiết bị phát broadcast.</p>
    </div>
    <div class="card">
      <h2>🌦 Vị trí thời tiết</h2>
      <div class="row">
        <label class="small muted">Vĩ độ</label><input id="s-lat" value="${st.weather.latitude}" style="width:110px">
        <label class="small muted">Kinh độ</label><input id="s-lon" value="${st.weather.longitude}" style="width:110px">
        <span class="small muted">(mở-meteo, miễn phí, không cần key)</span>
      </div>
    </div>
    <div class="card">
      <h2>🖥 Server</h2>
      ${toggle("s-mdns", st.server.advertise_mdns, "Thông báo server qua mDNS (app điện thoại tự tìm được) — áp dụng sau khi restart")}
      <p class="small muted">Đang chạy tại <code>http://${esc(st.server.lan_ip)}:${st.server.port}</code> · trang quản trị <code>/admin</code> · màn hiển thị <code>/</code></p>
      <div class="row">
        <label class="small muted">Đổi token quản trị</label>
        <input id="s-token" type="password" placeholder="token mới (≥ 6 ký tự)" style="width:200px">
        <span class="small muted">⚠️ các nơi đang đăng nhập khác sẽ phải nhập token mới.</span>
      </div>
    </div>
    <div class="card">
      <h2>🧹 Dọn dữ liệu tự động</h2>
      <div class="row">
        <label class="small muted">Giữ nhật ký</label>
        <input id="s-retention" type="number" min="1" value="${st.retention.days}" style="width:70px">
        <span class="small muted">ngày</span>
      </div>
      <p class="small muted">Chạy khi khởi động và mỗi ngày: xóa nhật ký cũ hơn số ngày trên,
      mã ghép nối đã dùng/hết hạn quá 1 ngày, hẹn giờ đã xong quá 7 ngày.</p>
    </div>
    <button class="primary" onclick="saveSettings()" style="padding:12px 28px">💾 Lưu cài đặt</button>`;
}

window.saveSettings = async () => {
  const body = {
    mqtt_enabled: $("#s-mqtt").checked,
    mqtt_host: $("#s-mqtt-host").value.trim() || null,
    mqtt_port: +$("#s-mqtt-port").value || null,
    mqtt_base_topic: $("#s-mqtt-topic").value.trim() || null,
    discovery_network_scan: $("#s-scan").checked,
    discovery_subnet: $("#s-subnet").value.trim() || "auto",
    weather_latitude: parseFloat($("#s-lat").value),
    weather_longitude: parseFloat($("#s-lon").value),
    advertise_mdns: $("#s-mdns").checked,
    retention_days: +$("#s-retention").value || null,
    admin_token: $("#s-token").value.trim() || null,
  };
  try {
    const res = await api("/settings", { method: "POST", body: JSON.stringify(body) });
    if (res.admin_token_changed) {
      const tok = $("#s-token").value.trim();
      token = tok;
      localStorage.setItem("sh_token", tok);
    }
    toast(res.changed.length ? "Đã lưu: " + res.changed.join("; ") : "Không có gì thay đổi");
    $("#s-token").value = "";
  } catch (e) { toast("Lỗi: " + e.message); }
};

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
