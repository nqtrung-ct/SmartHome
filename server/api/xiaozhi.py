"""Xiaozhi gateway: OTA/config endpoint + WebSocket voice cho thiết bị phòng.

Thiết bị (xiaozhi-esp32, vd. ES3C28P) boot -> POST /xiaozhi/ota/ (header
Device-Id/Client-Id) nhận về websocket url + token -> nối WS /xiaozhi/v1/
(hello, listen, binary Opus frames) theo docs/websocket.md của xiaozhi-esp32.

Đường tiếng: Opus 16k (up) -> PCM 16k -> endpointer (im lặng = hết câu, vì
mode "auto" của firmware stream liên tục và không báo "listen stop") ->
STT -> text_command -> TTS WAV -> resample 16k -> Opus 60ms (down).
Sau khi trả lời, giữ kênh mở FOLLOWUP_S giây cho câu hỏi tiếp (plan §12),
hết hạn thì đóng WS để thiết bị về idle chờ wake word.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import time
import uuid
from collections import OrderedDict

import av
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from server.api.devices import run_entity_command
from server.core.text_command import awaiting_confirmation, handle_command
from server.db import log_event
from server.util import normalize, now_iso
from server.voice.pipeline import _has_speech_energy

router = APIRouter()

SAMPLE_RATE = 16_000           # uplink: thiết bị gửi opus 16 kHz, STT chạy 16 kHz
OUT_RATE = 24_000               # downlink: khớp tốc độ phát của thiết bị (ES8311 24k)
FRAME_SAMPLES = 960             # 60 ms @ 16 kHz
BYTES_PER_SEC = SAMPLE_RATE * 2

PREROLL_S = 0.6        # giữ lại trước khi có tiếng để không cắt mất âm đầu
MIN_UTTERANCE_S = 0.8
MAX_UTTERANCE_S = 20.0
SILENCE_S = 1.2        # im lặng bao lâu thì coi là hết câu
TAIL_KEEP_S = 0.5
FOLLOWUP_S = 8.0       # cửa sổ nghe câu tiếp sau khi trả lời xong
MAX_AUTO_FOLLOWUPS = 5  # tối đa bao nhiêu lượt nối tiếp không có wake word
                      # (lượt vượt ngưỡng vẫn được đáp xong mới đóng phiên)
SEND_LEAD_S = 0.4      # packets TTS được phép dẫn trước phát tối đa chừng này
ECHO_GUARD_S = 0.5     # bỏ qua mic chừng này sau khi loa phát xong (1 mic, không AEC)
MIC_WARMUP_S = 0.45    # bỏ frame mic sau wake: tiếng bíp (~0.3s) + đuôi wake word khỏi lọt STT
WAKE_LISTEN_S = 10.0   # cửa sổ nghe dự phòng sau wake (nếu không có câu nào hoàn chỉnh)


# ---------------------------------------------------------------------------
# Opus helpers (PyAV; libopus decode luôn ra 48 kHz -> resample về 16 kHz s16)
# ---------------------------------------------------------------------------
class OpusDecoder:
    def __init__(self) -> None:
        self._codec = av.CodecContext.create("libopus", "r")
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

    def feed(self, packet: bytes) -> bytes:
        """Một packet opus -> PCM16 16k (có thể rỗng nếu decode ra nothing)."""
        out = bytearray()
        try:
            for frame in self._codec.decode(av.Packet(packet)):
                for res in self._resampler.resample(frame):
                    # to_ndarray(): đúng đúng số mẫu; bytes(plane) dính padding buffer
                    out.extend(res.to_ndarray().tobytes())
        except av.error.InvalidDataError:
            pass  # packet lỗi lúc vừa mở kênh - bỏ qua
        return bytes(out)


class OpusEncoder:
    def __init__(self, rate: int = OUT_RATE) -> None:
        self._rate = rate
        self._codec = av.CodecContext.create("libopus", "w")
        self._codec.sample_rate = rate
        self._codec.format = "s16"
        self._codec.layout = "mono"
        # frame_size chỉ set được qua options (thuộc tính chỉ đọc trong PyAV);
        # libopus tự gói 20 ms/packet - đủ mịn cho pacing
        self._codec.options = {"application": "voip", "frame_size": str(FRAME_SAMPLES)}
        self._codec.open()
        self._buf = bytearray()

    def encode(self, pcm16: bytes) -> tuple[list[bytes], float]:
        """PCM16 mono (theo self._rate) -> (packets opus, tổng thời lượng giây)."""
        self._buf.extend(pcm16)
        del self._buf[: len(self._buf) % 2]  # chẵn byte cho sample 16-bit
        packets: list[bytes] = []
        while len(self._buf) >= FRAME_SAMPLES * 2:
            chunk = bytes(self._buf[: FRAME_SAMPLES * 2])
            del self._buf[: FRAME_SAMPLES * 2]
            frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.sample_rate = self._rate
            frame.planes[0].update(chunk)
            for pkt in self._codec.encode(frame):
                if pkt.size > 0:  # bỏ packet rỗng (DTX)
                    packets.append(bytes(pkt))
        return packets, len(pcm16) / 2 / self._rate

    def flush(self) -> list[bytes]:
        return [bytes(p) for p in self._codec.encode(None) if p.size > 0]

    def encode_all(self, pcm16: bytes) -> list[bytes]:
        """Mọi packets cho một đoạn PCM (gồm flush)."""
        pkts, _ = self.encode(pcm16)
        return pkts + self.flush()


def wav_to_pcm(wav: bytes, rate: int) -> bytes:
    """WAV PCM mono bất kỳ -> PCM16 mono đúng rate (TTS piper là 22050 Hz)."""
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as w:
        src_rate = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sampwidth != 2:
        buf = io.BytesIO()
        with av.open(io.BytesIO(wav)) as src, av.open(buf, "w", format="s16le") as dst:
            stream = dst.add_stream("pcm_s16le", rate=rate, layout="mono")
            for frame in src.decode(audio=0):
                for f in stream.encode(frame):
                    dst.mux(f)
            for f in stream.encode(None):
                dst.mux(f)
        return buf.getvalue()
    pcm = raw
    if channels != 1:
        pcm = bytes(memoryview(pcm).cast("h")[::channels])
    if src_rate == rate:
        return pcm
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.sample_rate = src_rate
    frame.planes[0].update(pcm)
    # to_ndarray(): đúng số mẫu; bytes(plane) dính padding buffer
    out = [f.to_ndarray().tobytes() for f in resampler.resample(frame)]
    out += [f.to_ndarray().tobytes() for f in (resampler.resample(None) or [])]
    return b"".join(out)


# ---------------------------------------------------------------------------
# Endpointing: thiết bị stream liên tục, server tự nhận hết câu
# ---------------------------------------------------------------------------
# Thiết bị gửi kèm âm wake word (SEND_WAKE_WORD_DATA) nên STT hay nghe "Alexa"
# thành 'alexa'/'elect'/... đứng đầu câu - cắt bỏ trước khi đưa vào não lệnh.
_ALEXA_PREFIX = re.compile(
    r"^(?:alexa|ale xa|a le xa|e le xa|elexa|elect|a lax a|al exa)\b[\s,.!?]*"
)

# tiếng đệm người Việt hay mở đầu câu nói ("ủa chắc tivi", "ừm để nghĩ...", STT
# nghe 'hãy' thành 'ấy') — bỏ bớt để não lệnh không bị nhiễu; chỉ bỏ khi còn
# lại nội dung thật
_FILLER_PREFIX = re.compile(r"^(?:ủa|ùa|à|á|ừ|ừm|um|ơ|ấy|ay)\s*[,.\s]*", flags=re.I)


def strip_wake_word(transcript: str, wake: str | None = None) -> str | None:
    """Câu đã bớt wake word; '' nếu chỉ toàn wake word; None nếu không có wake word.

    wake = cụm từ wake device vừa báo qua message "listen detect" (vd "Hi Andy")
    — dùng để cắt đúng tiền tố khi người dùng đổi từ đánh thức trong Settings.
    """
    norm = normalize(transcript).strip()
    m = _ALEXA_PREFIX.match(norm)
    if m:
        return norm[m.end():].strip(" .,?!")
    if wake:
        wp = normalize(wake).strip()
        if wp and norm.startswith(wp):
            return norm[len(wp):].strip(" .,?!")
    return None


class UtteranceEndpointer:
    """Gom PCM thành một câu: bắt đầu khi có tiếng, kết thúc khi im SILENCE_S.

    Im lặng tính theo số mẫu âm thanh (không theo đồng hồ) để đúng cả khi
    packets tới dồn cụm vì mạng.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._preroll = bytearray()
        self.utterance = bytearray()
        self._capturing = False
        self._samples = 0            # tổng mẫu đã feed trong câu
        self._last_speech_sample = 0

    def feed(self, pcm: bytes) -> bytes | None:
        """Một chunk PCM16 in; trả về utterance PCM khi một câu hoàn chỉnh."""
        n = len(pcm) // 2
        if not self._capturing:
            if not _has_speech_energy(pcm):
                self._preroll.extend(pcm)
                del self._preroll[: -int(PREROLL_S * BYTES_PER_SEC) * 2]
                return None
            self._capturing = True
            self.utterance = bytearray(self._preroll) + pcm
            self._preroll = bytearray()
            self._samples = len(self.utterance) // 2
            self._last_speech_sample = self._samples
            return None
        self.utterance.extend(pcm)
        self._samples += n
        if _has_speech_energy(pcm):
            self._last_speech_sample = self._samples
        silence_samples = self._samples - self._last_speech_sample
        dur = self._samples / SAMPLE_RATE
        if silence_samples >= int(SILENCE_S * SAMPLE_RATE) and dur >= MIN_UTTERANCE_S:
            return self._finish()
        if dur >= MAX_UTTERANCE_S:
            return self._finish()
        return None

    def _finish(self) -> bytes:
        # bỏ đuôi im lặng, chừa lại một chút cho tự nhiên
        keep = (self._last_speech_sample + int(TAIL_KEEP_S * SAMPLE_RATE)) * 2
        keep &= ~3  # PCM16 stereo-align an toàn
        pcm = bytes(self.utterance[: max(keep, int(MIN_UTTERANCE_S * BYTES_PER_SEC))])
        self.reset()
        return pcm


# ---------------------------------------------------------------------------
# Kết nối đang sống (dùng sau cho announce đẩy từ scheduler)
# ---------------------------------------------------------------------------
class XiaozhiConnections:
    def __init__(self) -> None:
        self._live: dict[int, WebSocket] = {}
        # thông báo chờ phát (hẹn giờ đến...): thiết bị idle đóng WS nên chỉ
        # có thể giao qua màn hình poll GET /xiaozhi/ui (30 giây/lần)
        self._pending: dict[int, list[tuple[str, float]]] = {}

    def register(self, device_row_id: int, ws: WebSocket) -> None:
        self._live[device_row_id] = ws

    def unregister(self, device_row_id: int) -> None:
        self._live.pop(device_row_id, None)

    def get(self, device_row_id: int) -> WebSocket | None:
        return self._live.get(device_row_id)

    def enqueue_announce(self, device_row_id: int, text: str) -> None:
        self._pending.setdefault(device_row_id, []).append((text, time.monotonic()))

    def take_pending(self, device_row_id: int, max_age_s: float = 600.0) -> list[str]:
        """Thông báo còn tươi cho thiết bị; quá cũ bỏ luôn (nói lại giờ này chỉ rối)."""
        queue = self._pending.pop(device_row_id, [])
        fresh = [text for text, ts in queue if time.monotonic() - ts <= max_age_s]
        return fresh


connections = XiaozhiConnections()


async def announce_room(app, room_id, text: str) -> None:
    """Đưa thông báo (hẹn giờ đến) tới các thiết bị phòng: WS chỉ mở khi người
    dùng đang nói chuyện nên không đẩy thẳng được (loa + một mic không AEC sẽ
    tự nghe chính mình) — để pending cho màn hình poll lượm qua audio_url.
    Timer tạo mà không thuộc phòng nào (admin/API) thì phát mọi thiết bị."""
    db = app.state.db
    if room_id is None:
        rows = db.query("SELECT id, name FROM xiaozhi_devices")
    else:
        rows = db.query("SELECT id, name FROM xiaozhi_devices WHERE room_id=?", (room_id,))
    for row in rows:
        connections.enqueue_announce(row["id"], text)
        log_event(db, "xiaozhi", f"Thông báo chờ phát trên '{row['name']}': {text}")


# ---------------------------------------------------------------------------
# OTA / config endpoint
# ---------------------------------------------------------------------------
def _get_or_create_device(db, device_id: str, client_id: str, info: dict):
    row = db.one("SELECT * FROM xiaozhi_devices WHERE device_id=?", (device_id,))
    if row is None:
        name = f"Thiết bị phòng {device_id[-5:].replace(':', '').upper()}"
        db.execute(
            "INSERT INTO xiaozhi_devices(device_id, client_id, name, token, info, created_at, last_seen)"
            " VALUES(?,?,?,?,?,?,?)",
            (device_id, client_id, name, secrets.token_hex(16),
             json.dumps(info, ensure_ascii=False), now_iso(), now_iso()),
        )
        row = db.one("SELECT * FROM xiaozhi_devices WHERE device_id=?", (device_id,))
        log_event(db, "xiaozhi", f"Thiết bị mới chờ gán phòng: {name} ({device_id})")
    else:
        db.execute(
            "UPDATE xiaozhi_devices SET last_seen=?, client_id=?, info=? WHERE id=?",
            (now_iso(), client_id or row["client_id"],
             json.dumps(info, ensure_ascii=False) if info else row["info"], row["id"]),
        )
        row = db.one("SELECT * FROM xiaozhi_devices WHERE id=?", (row["id"],))
    return row


@router.api_route("/xiaozhi/ota/", methods=["GET", "POST"])
async def xiaozhi_ota(request: Request):
    db = request.app.state.db
    cfg = request.app.state.cfg
    device_id = (request.headers.get("Device-Id") or "").strip()
    client_id = (request.headers.get("Client-Id") or "").strip()
    if not device_id:
        return {"error": "missing Device-Id"}
    info: dict = {}
    if request.method == "POST":
        try:
            info = await request.json()
        except Exception:
            info = {}
    row = _get_or_create_device(db, device_id, client_id, info)

    from server.main import lan_ip

    return {
        "server_time": {
            "timestamp": int(time.time() * 1000),
            "timezone_offset": 420,  # phút; firmware: ts += offset*60*1000
        },
        "websocket": {
            "url": f"ws://{lan_ip()}:{int(cfg.get('server.port', 8124))}/xiaozhi/v1/",
            "token": row["token"],
        },
        # không trả "firmware" = không có bản mới; không trả "mqtt" = dùng websocket
    }


# ---------------------------------------------------------------------------
# UI màn hình thiết bị: trạng thái phòng đẩy xuống + lệnh từ nút bấm
# ---------------------------------------------------------------------------
# Thiết bị idle có WS đóng (chế độ riêng tư: mic chỉ mở sau wake word) nên
# màn hình tự poll GET /xiaozhi/ui/<mac>; khi WS đang mở thì server chủ động
# đẩy {"type":"ui_state"} sau mỗi lệnh. Nút bấm POST entity_id trực tiếp
# (không qua NLU) và nhận về reply dạng OGG để phát bằng NotifyPlayer.
UI_MAX_DEVICES = 6
UI_TYPES = ("climate", "fan", "light", "switch", "media")  # thứ tự ưu tiên hiển thị

# cache OGG phản hồi nút bấm: id -> bytes (LRU nhỏ, hết giờ tự xóa)
_OGG_CACHE: OrderedDict[str, bytes] = OrderedDict()
_OGG_TTL_S = 600.0


def _ui_devices(db, room_id: int) -> list[dict]:
    """Các thực thể bật/tắt được trong phòng, sắp theo loại rồi tên."""
    rows = db.query(
        """SELECT e.id, e.name, e.entity_type, e.state, e.driver_data
           FROM entities e JOIN devices d ON d.id=e.device_id
           WHERE e.entity_type IN ('climate','fan','light','switch','media')
             AND COALESCE(e.room_id, d.room_id)=?""",
        (room_id,),
    )
    devs = []
    for e in rows:
        try:
            dd = json.loads(e["driver_data"] or "{}")
        except ValueError:
            dd = {}
        if dd.get("readonly"):
            continue
        try:
            state = json.loads(e["state"] or "{}")
        except ValueError:
            state = {}
        item = {"id": e["id"], "name": e["name"], "on": state.get("state") == "on"}
        if e["entity_type"] == "climate" and state.get("temperature") is not None:
            item["d"] = f"{state['temperature']:g}°"
        devs.append((e["entity_type"], item))
    devs.sort(key=lambda t: (UI_TYPES.index(t[0]) if t[0] in UI_TYPES else len(UI_TYPES),
                             t[1]["name"]))
    return [item for _t, item in devs][:UI_MAX_DEVICES]


def _ui_timers(db, room_id: int) -> list[dict]:
    """2 hẹn giờ gần nhất của phòng cho card trên màn hình thiết bị."""
    from server.util import now, parse_iso

    out = []
    for t in db.query(
        """SELECT target_at, message, repeat FROM timers
           WHERE done=0 AND (room_id IS NULL OR room_id=?)
           ORDER BY target_at LIMIT 2""",
        (room_id,),
    ):
        at = parse_iso(t["target_at"])
        when = at.strftime("%H:%M")
        if t["repeat"]:
            when = f"{when} lặp lại"
        else:
            days = (at.date() - now().date()).days
            if days == 1:
                when = f"mai {when}"
            elif days > 1:
                when = f"{at.day}/{at.month} {when}"
        out.append({"t": when, "m": (t["message"] or "hẹn giờ").strip()[:40]})
    return out


def build_ui_state(app, room_id: int | None) -> dict:
    """Trạng thái hiển thị trên màn hình thiết bị phòng (server = nguồn chân lý)."""
    db = app.state.db
    payload: dict = {"room": None, "temp": None, "hum": None, "solar": None,
                     "weather": None, "devices": [], "timers": [],
                     "vol": int(app.state.cfg.get("xiaozhi.volume", 100)),
                     "speed": float(app.state.cfg.get("tts.length_scale", 1.15))}
    if room_id:
        room = db.one("SELECT name FROM rooms WHERE id=?", (room_id,))
        payload["room"] = room["name"] if room else None
        for e in db.query(
            """SELECT e.state, e.updated_at FROM entities e
               JOIN devices d ON d.id=e.device_id
               WHERE e.entity_type='sensor'
                 AND COALESCE(e.room_id, d.room_id)=?""",
            (room_id,),
        ):
            try:
                state = json.loads(e["state"] or "{}")
            except ValueError:
                continue
            if payload["temp"] is None and state.get("temperature") is not None:
                payload["temp"] = round(float(state["temperature"]), 1)
            if payload["hum"] is None and state.get("humidity") is not None:
                payload["hum"] = int(state["humidity"])
        payload["devices"] = _ui_devices(db, room_id)
        payload["timers"] = _ui_timers(db, room_id)
    solar = db.one(
        """SELECT e.state FROM entities e JOIN devices d ON d.id=e.device_id
           WHERE d.driver='senergy' AND e.entity_type='sensor' LIMIT 1"""
    )
    if solar:
        try:
            soc = json.loads(solar["state"] or "{}").get("battery_soc")
        except ValueError:
            soc = None
        if soc is not None:
            payload["solar"] = int(soc)
    try:
        snap = app.state.weather.snapshot()
        payload["weather"] = {"t": snap.get("temperature"), "c": snap.get("condition")}
    except Exception:
        pass
    return payload


def _pcm_to_ogg_opus(pcm: bytes, rate: int = OUT_RATE) -> bytes:
    """PCM16 mono -> OGG Opus (định dạng NotifyPlayer phát được)."""
    buf = io.BytesIO()
    output = av.open(buf, "w", format="ogg")
    stream = output.add_stream("libopus", rate=rate, layout="mono")
    for i in range(0, len(pcm) - 1, FRAME_SAMPLES * 2):
        chunk = pcm[i:i + FRAME_SAMPLES * 2]
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(chunk) // 2)
        frame.sample_rate = rate
        frame.planes[0].update(chunk)
        for pkt in stream.encode(frame):
            output.mux(pkt)
    for pkt in stream.encode(None):
        output.mux(pkt)
    output.close()
    return buf.getvalue()


def _beep_preamble(rate: int = OUT_RATE) -> bytes:
    """3 bíp 1 kHz cách nhau 0.5 giây — thu hút chú ý trước thông báo hẹn giờ."""
    import math

    beep_s, period_s, n_beeps, tail_s = 0.15, 0.5, 3, 0.25
    total = int((period_s * (n_beeps - 1) + beep_s + tail_s) * rate)
    pcm = bytearray(total * 2)
    amp = 0.55 * 32767
    for b in range(n_beeps):
        start = int(b * period_s * rate)
        n = int(beep_s * rate)
        fade = max(int(0.01 * rate), 1)  # attack/decay chống tiếng 'chát' đầu cuối bíp
        for i in range(n):
            env = min(1.0, min(i, n - i) / fade)
            v = int(amp * env * math.sin(2 * math.pi * 1000 * i / rate))
            off = (start + i) * 2
            pcm[off:off + 2] = v.to_bytes(2, "little", signed=True)
    return bytes(pcm)


async def _tts_ogg_url(app, text: str, attention: bool = False) -> str | None:
    """Phát câu reply qua loa thiết bị idle: TTS -> OGG Opus -> URL tạm.

    attention=True ghép 3 bíp trước giọng nói (thông báo hẹn giờ cần gây chú ý)."""
    spoken = await app.state.pipeline.tts_audio(text)
    if not spoken:
        log_event(app.state.db, "xiaozhi",
                  "Không tạo được tiếng thông báo (TTS không trả audio)", level="warn")
        return None
    pcm = wav_to_pcm(spoken[0], OUT_RATE)
    if attention:
        pcm = _beep_preamble() + pcm
    try:
        ogg = _pcm_to_ogg_opus(pcm)
    except Exception as exc:
        log_event(app.state.db, "xiaozhi", f"Lỗi tạo OGG phát: {exc}", level="warn")
        return None
    audio_id = secrets.token_hex(8)
    now = time.monotonic()
    for k in [k for k, (b, ts) in _OGG_CACHE.items() if now - ts > _OGG_TTL_S]:
        _OGG_CACHE.pop(k, None)
    _OGG_CACHE[audio_id] = (ogg, now)
    while len(_OGG_CACHE) > 8:
        _OGG_CACHE.popitem(last=False)
    from server.main import lan_ip

    port = int(app.state.cfg.get("server.port", 8124))
    return f"http://{lan_ip()}:{port}/xiaozhi/audio/{audio_id}.ogg"


@router.get("/xiaozhi/audio/{audio_id}.ogg")
async def xiaozhi_audio(audio_id: str):
    entry = _OGG_CACHE.get(audio_id)
    if entry is None or time.monotonic() - entry[1] > _OGG_TTL_S:
        raise HTTPException(status_code=404)
    return Response(entry[0], media_type="audio/ogg")


@router.api_route("/xiaozhi/ui/{device_id}", methods=["GET", "POST"])
async def xiaozhi_ui(request: Request, device_id: str):
    """Màn hình thiết bị: GET = trạng thái phòng; POST = lệnh từ nút bấm."""
    app = request.app
    db = app.state.db
    row = db.one("SELECT * FROM xiaozhi_devices WHERE device_id=?", (device_id,))
    if row is None:
        raise HTTPException(status_code=404)
    room_id = row["room_id"]

    if request.method == "GET":
        payload = build_ui_state(app, room_id)
        pending = connections.take_pending(row["id"])
        if pending:
            text = " ".join(pending)
            url = await _tts_ogg_url(app, text, attention=True)
            if url:
                # firmware phát qua NotifyPlayer (mic đang tắt ở chế độ idle)
                payload["reply"] = text
                payload["audio_url"] = url
            else:
                # TTS hỏng đúng lúc này — xếp lại chờ lần poll sau, đừng mất thông báo
                connections.enqueue_announce(row["id"], text)
        return payload

    try:
        body = await request.json()
    except Exception:
        body = {}
    reply = None
    if isinstance(body.get("volume"), (int, float)) and 0 <= body["volume"] <= 100:
        # âm lượng từ popup Cài đặt: lưu config, firmware đã áp dụng tại chỗ
        from server.util import set_yaml_value

        app.state.cfg._data.setdefault("xiaozhi", {})["volume"] = int(body["volume"])
        set_yaml_value(app.state.cfg.path, "xiaozhi", "volume", int(body["volume"]))
        log_event(db, "xiaozhi", f"[cài đặt] Âm lượng loa: {int(body['volume'])}")
        return {"ok": True, "state": build_ui_state(app, room_id)}
    elif isinstance(body.get("speed"), (int, float)) and 0.5 <= body["speed"] <= 2.0:
        from server.util import set_yaml_value

        app.state.cfg._data.setdefault("tts", {})["length_scale"] = float(body["speed"])
        set_yaml_value(app.state.cfg.path, "tts", "length_scale", float(body["speed"]))
        log_event(db, "xiaozhi", f"[cài đặt] Tốc độ đọc: {body['speed']:.2f}x")
        return {"ok": True, "state": build_ui_state(app, room_id)}
    elif isinstance(body.get("entity_id"), int):
        entity = db.one("SELECT name FROM entities WHERE id=?", (body["entity_id"],))
        current = build_ui_state(app, room_id)
        now_on = any(d["id"] == body["entity_id"] and d["on"] for d in current["devices"])
        state = body.get("state") or ("off" if now_on else "on")
        try:
            await run_entity_command(app, body["entity_id"], state, {})
            verb = "bật" if state == "on" else "tắt"
            reply = f"Đã {verb} {entity['name'] if entity else 'thiết bị'}."
        except HTTPException as exc:
            reply = str(exc.detail)
    elif body.get("text"):
        result = await handle_command(
            db, app.state.drivers, app.state.cfg, app.state.weather,
            normalize(body["text"]), room_id,
        )
        reply = result["reply"]

    audio_url = await _tts_ogg_url(app, reply) if reply else None
    if reply:
        log_event(db, "xiaozhi", f"[nút] -> {reply}")
    return {"reply": reply, "audio_url": audio_url, "state": build_ui_state(app, room_id)}


# ---------------------------------------------------------------------------
# WebSocket voice endpoint
# ---------------------------------------------------------------------------
@router.websocket("/xiaozhi/v1/")
async def xiaozhi_ws(ws: WebSocket):
    token = (ws.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    db = ws.app.state.db
    row = db.one("SELECT * FROM xiaozhi_devices WHERE token=?", (token,))
    if row is None:
        await ws.close(code=4401)
        return
    await ws.accept()

    app = ws.app
    pipeline = app.state.pipeline
    device_row_id, room_id = row["id"], row["room_id"]
    connections.register(device_row_id, ws)
    db.execute("UPDATE xiaozhi_devices SET last_seen=? WHERE id=?", (now_iso(), device_row_id))
    log_event(db, "xiaozhi", f"'{row['name']}' đã kết nối (phòng {room_id})")

    session_id = uuid.uuid4().hex
    decoder = OpusDecoder()
    endpointer = UtteranceEndpointer()
    listening = False
    last_wake = 0.0                # thời điểm wake/bắt đầu nghe — để phân biệt đuôi wake word với rác thật
    mic_warmup_until = 0.0         # bỏ mic chừng này sau wake (thời gian bíp + đuôi wake word)
    wake_phrase: str | None = None  # wake word device vừa báo qua "listen detect"
    speaking_task: asyncio.Task | None = None
    close_at: float | None = None  # hết cửa sổ follow-up -> đóng kênh
    ignore_until = 0.0             # chống vọng: bỏ mic chừng này sau khi loa phát xong
    auto_followups = 0             # số lệnh nối tiếp không có wake word trong phiên
    mcp_id = 0

    async def send_json(data: dict) -> None:
        data.setdefault("session_id", session_id)
        await ws.send_json(data)

    async def call_tool(name: str, arguments: dict) -> None:
        """Gọi công cụ MCP trên thiết bị (âm lượng, độ sáng...)."""
        nonlocal mcp_id
        mcp_id += 1
        await send_json({
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
                "id": mcp_id,
            },
        })

    async def speak(reply: str) -> float:
        """Phát một câu qua loa thiết bị; trả về thời lượng (giây) đã phát."""
        spoken = await pipeline.tts_audio(reply)
        await send_json({"type": "tts", "state": "start"})
        duration = 0.0
        if spoken:
            enc = OpusEncoder()
            pcm = wav_to_pcm(spoken[0], OUT_RATE)
            pkts, dur = enc.encode(pcm)
            pkts += enc.flush()
            t0 = time.monotonic()
            sent = 0.0
            for pkt in pkts:
                await ws.send_bytes(pkt)
                sent += dur / len(pkts)
                lead = sent - (time.monotonic() - t0)
                if lead > SEND_LEAD_S:  # gửi theo nhịp thực, chỉ dẫn trước một chút
                    await asyncio.sleep(lead - SEND_LEAD_S)
            duration = dur
            # tts stop chỉ được gửi khi thiết bị kịp phát XONG audio cuối -
            # gửi sớm là firmware cắt mất đuôi câu (đã trả giá)
            remaining = dur - (time.monotonic() - t0) + 0.15
            if remaining > 0:
                await asyncio.sleep(remaining)
        await send_json({"type": "tts", "state": "stop"})
        return duration

    async def respond(pcm: bytes) -> None:
        nonlocal close_at, speaking_task, ignore_until, auto_followups
        try:
            transcript = await asyncio.to_thread(pipeline.stt.transcribe, pcm)
        except Exception as exc:
            log_event(db, "xiaozhi", f"Lỗi STT: {exc}", level="warn")
            transcript = ""
        stripped = strip_wake_word(transcript, wake_phrase)
        # giữ bản có dấu cho não lệnh (lời nhắc hẹn giờ, câu trả lời có dấu
        # tiếng Việt) — handle_command tự normalize khi so khớp
        command = transcript.strip() if stripped is None else stripped
        # bỏ tiếng đệm mở đầu ("ủa chắc tivi" -> "chắc tivi"); toàn tiếng đệm
        # thì giữ nguyên kẻo chào lại tiếng TV
        cleaned = _FILLER_PREFIX.sub("", command).strip()
        if cleaned:
            command = cleaned
        if not command:  # chỉ toàn wake word -> im lặng (tiếng bíp của thiết bị
            # đã là tín hiệu sẵn sàng), chỉ giữ cửa sổ nghe cho câu tiếp theo
            auto_followups = 0  # người dùng chủ động đánh thức lại
            close_at = time.monotonic() + FOLLOWUP_S
        elif (len(command) <= 3 or " " not in command and len(command) <= 6) \
                and not awaiting_confirmation(room_id):
            # mảnh rác/vọng âm ('đây', 'đó', 'plaska') -> im lặng và KHÔNG gia hạn
            # cửa sổ nghe, kẻo tiếng TV/tiếng nền tự hỏi-đáp thành chuỗi không dứt.
            # Trừ khi đang chờ xác nhận hủy hẹn giờ — 'có'/'ok' phải đi qua.
            log_event(db, "xiaozhi", f"Bỏ qua câu rác: '{transcript}'")
            if time.monotonic() - last_wake < 3.0:
                # ngay sau wake word, đuôi âm thường chỉ nghe được 'ủa...' —
                # đó chưa phải là rác thật, giữ nguyên cửa sổ chờ lệnh chính
                close_at = time.monotonic() + FOLLOWUP_S
            else:
                close_at = time.monotonic() + 2.0
        else:
            # chạm ngưỡng lượt nối tiếp: vẫn ĐÁP lệnh này xong mới đóng phiên —
            # vứt câu lệnh thật chỉ vì hết suất nối tiếp là trải nghiệm tệ
            capped = auto_followups >= MAX_AUTO_FOLLOWUPS
            auto_followups += 1
            try:
                result = await handle_command(
                    db, app.state.drivers, app.state.cfg, app.state.weather, command, room_id,
                )
                await send_json({"type": "stt", "text": transcript})
                duration = await speak(result["reply"])
                ignore_until = time.monotonic() + duration + ECHO_GUARD_S
                log_event(
                    db, "xiaozhi",
                    f"[{result.get('engine')}] '{transcript}' -> {result['reply']}",
                )
                # lệnh vừa chạy có thể đổi trạng thái phòng -> đẩy lại cho màn hình
                await send_json({"type": "ui_state", "payload": build_ui_state(app, room_id)})
            except Exception as exc:  # không để thiết bị treo listening không phản hồi
                log_event(db, "xiaozhi", f"Lỗi xử lý lệnh: {exc}", level="warn")
                await speak("Xin lỗi, mình bị lỗi rồi.")
            if capped:
                log_event(db, "xiaozhi",
                          f"Đóng phiên sau {auto_followups} lượt không wake word")
                close_at = time.monotonic()  # đáp xong lệnh cuối -> hết phiên
            else:
                close_at = time.monotonic() + FOLLOWUP_S  # nghe thêm câu tiếp chừng ấy giây
        speaking_task = None

    def finish_speaking() -> None:
        nonlocal speaking_task
        if speaking_task:
            speaking_task.cancel()
            speaking_task = None

    try:
        while True:
            # chờ message, nhưng tự đóng khi hết cửa sổ follow-up
            timeout = None
            if close_at is not None and listening and not speaking_task:
                timeout = max(0.05, close_at - time.monotonic())
            try:
                message = await asyncio.wait_for(ws.receive(), timeout) if timeout else await ws.receive()
            except asyncio.TimeoutError:
                await ws.close()
                break
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                if listening and not speaking_task \
                        and time.monotonic() >= ignore_until \
                        and time.monotonic() >= mic_warmup_until:
                    pcm = decoder.feed(message["bytes"])
                    done = endpointer.feed(pcm) if pcm else None
                    if done:
                        endpointer.reset()
                        finish_speaking()
                        speaking_task = asyncio.create_task(respond(done))
                        close_at = None
                continue

            data: dict = {}
            if message.get("text"):
                try:
                    data = json.loads(message["text"])
                except ValueError:
                    continue
            msg_type = data.get("type")

            if msg_type == "hello":
                await send_json({
                    "type": "hello",
                    "transport": "websocket",
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": OUT_RATE,  # khớp tốc độ phát của thiết bị
                        "channels": 1,
                        "frame_duration": 60,
                    },
                })
                # server là nguồn chân lý: áp âm lượng cấu hình cho mỗi phiên
                volume = int(app.state.cfg.get("xiaozhi.volume", 100))
                if 0 <= volume <= 100:
                    try:
                        await call_tool("self.audio_speaker.set_volume", {"volume": volume})
                    except Exception:
                        pass
                await send_json({"type": "ui_state", "payload": build_ui_state(app, room_id)})

            elif msg_type == "listen":
                state = data.get("state")
                if state in ("start", "detect"):
                    listening = True
                    last_wake = time.monotonic()
                    # mic đã được chắn lúc đầu nên đuôi wake word không còn tạo
                    # thành câu rác -> cần cửa sổ dự phòng để phiên tự đóng nếu
                    # người dùng không nói gì
                    close_at = time.monotonic() + WAKE_LISTEN_S
                    mic_warmup_until = time.monotonic() + MIC_WARMUP_S
                    if state == "detect" and data.get("text"):
                        wake_phrase = data["text"]
                    decoder = OpusDecoder()
                    endpointer.reset()
                    finish_speaking()  # người nói chen ngang khi đang phát
                elif state == "stop":
                    # chỉ đến từ nút bấm (mode manual) - dừng nghe, trả về ngay nếu có
                    listening = False
                    pcm = endpointer.utterance
                    endpointer.reset()
                    finish_speaking()
                    if len(pcm) >= MIN_UTTERANCE_S * BYTES_PER_SEC:
                        speaking_task = asyncio.create_task(respond(bytes(pcm)))

            elif msg_type == "abort":
                listening = False
                finish_speaking()
                decoder = OpusDecoder()
                endpointer.reset()

            elif msg_type == "mcp":
                pass  # công cụ trên thiết bị (âm lượng, màn hình...) - làm ở mốc MCP

            elif msg_type == "goodbye":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log_event(db, "xiaozhi", f"Lỗi phiên thoại: {exc}", level="warn")
    finally:
        finish_speaking()
        connections.unregister(device_row_id)
        db.execute("UPDATE xiaozhi_devices SET last_seen=? WHERE id=?", (now_iso(), device_row_id))
        log_event(db, "xiaozhi", f"'{row['name']}' ngắt kết nối")
