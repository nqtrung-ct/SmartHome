// Wall display: read-only household dashboard. Refreshes every 30 s.
"use strict";

const $ = (id) => document.getElementById(id);

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

async function refresh() {
  let data;
  try {
    const resp = await fetch("/api/display");
    if (!resp.ok) throw new Error(resp.status);
    data = await resp.json();
  } catch {
    $("updated").textContent = "Mất kết nối server — thử lại…";
    return;
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

  // hero
  $("h-on").textContent = data.counts.on_now;
  $("h-rooms").textContent = data.counts.rooms;
  $("h-timers").textContent = data.timers.length;

  // rooms
  const rooms = data.rooms.filter((r) => r.entity_count > 0);
  $("room-list").innerHTML = rooms.length
    ? rooms.map((r) => `
        <div class="room">
          <span class="dot ${r.active ? "on" : ""}"></span>
          <div>
            <div class="r-name">${esc(r.name)}${r.floor ? `<span class="badge">${esc(r.floor)}</span>` : ""}</div>
            <div class="r-sub">${r.lights_on > 0 ? `${r.lights_on} thiết bị đang bật` : "không có gì đang bật"}</div>
          </div>
          <div class="r-right">
            ${r.temperature != null ? `<div class="r-temp">${r.temperature}°C</div>` : ""}
            ${r.humidity != null ? `<div class="r-hum">💧 ${r.humidity}%</div>` : ""}
          </div>
        </div>`).join("")
    : `<p class="muted">Chưa có phòng nào có thiết bị.</p>`;

  // devices
  $("d-lights").textContent = data.counts.on_now; // lights+switches currently on
  $("d-ac").textContent = data.counts.climates;
  $("d-sensors").textContent = data.counts.sensors;
  $("d-cams").textContent = data.counts.cameras;

  // timers
  $("timer-list").innerHTML = data.timers.length
    ? data.timers.map((t) => {
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

  $("updated").textContent = "Cập nhật " + data.updated_at;
}

refresh();
setInterval(refresh, 30000);
