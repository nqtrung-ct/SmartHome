"""WebSocket voice gateway: audio in -> transcript/reply/audio out.

Protocol (push-to-talk v1):
  client -> server: {"type": "start"} | binary PCM16 16kHz mono frames | {"type": "stop"}
  server -> client: {"type": "transcript", ...} {"type": "reply", ...}
                    {"type": "audio", "sample_rate": N} + binary chunks + {"type": "end"}
  announcements (timers) arrive as {"type": "announce", ...} + audio + end.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from server.api.deps import voice_identity
from server.db import log_event

router = APIRouter()

MAX_AUDIO_BYTES = 16_000 * 2 * 60  # 60 seconds of 16 kHz PCM16


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket, token: str = Query("")):
    await ws.accept()
    identity = voice_identity(ws.app, token)
    if not identity:
        await ws.close(code=4401)
        return

    app = ws.app
    db = app.state.db
    connections = app.state.voice_connections
    pipeline = app.state.pipeline
    client_id = identity["client_id"]
    connections.register(client_id, ws)
    log_event(db, "voice", f"Máy '{identity['name']}' đã kết nối")

    buffer = bytearray()
    capturing = False
    busy = False

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                if capturing and len(buffer) < MAX_AUDIO_BYTES:
                    buffer.extend(message["bytes"])
                continue

            data = {}
            if message.get("text"):
                try:
                    data = json.loads(message["text"])
                except ValueError:
                    data = {}
            msg_type = data.get("type")

            if msg_type == "ping":
                await ws.send_json({"type": "pong"})

            elif msg_type == "start":
                buffer.clear()
                capturing = True
                await ws.send_json({"type": "ready"})

            elif msg_type == "stop" and not busy:
                capturing = False
                busy = True
                try:
                    audio = bytes(buffer)
                    buffer.clear()
                    if not audio:
                        await ws.send_json({"type": "end"})
                        continue
                    result = await pipeline.process_audio(audio, identity["room_id"])
                    await connections.send_audio(
                        ws, "transcript", result.get("transcript", ""), None
                    )
                    spoken = await pipeline.tts_audio(result["reply"])
                    await connections.send_audio(ws, "reply", result["reply"], spoken)
                    log_event(
                        db, "voice",
                        f"[{result.get('engine')}] '{result.get('transcript', '')}' -> {result['reply']}",
                    )
                finally:
                    busy = False
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log_event(db, "voice", f"Lỗi kết nối thoại: {exc}", level="warn")
    finally:
        connections.unregister(client_id)
        log_event(db, "voice", f"Máy '{identity['name']}' ngắt kết nối")
