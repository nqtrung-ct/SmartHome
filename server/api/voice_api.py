"""WebSocket voice gateway: audio in -> transcript/reply/audio out.

Protocol:
  push-to-talk: {"type":"start"} | binary PCM16 16kHz mono frames | {"type":"stop"}
  hands-free:   {"type":"handsfree","silence_ms":3000} then stream mic audio
                continuously. Server wakes on "trợ lý ơi", ends the utterance
                on the "over" keyword or on silence, replies with TTS, then
                resumes listening ({"type":"listening"}). {"type":"handsfree_off"} exits.
  server -> client: {"type":"ready"|"transcript"|"reply"|"ignored"|"listening", ...}
                    {"type":"audio", "sample_rate": N} + binary chunks + {"type":"end"}
  announcements (timers) arrive as {"type":"announce", ...} + audio + end.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from server.api.deps import voice_identity
from server.core.text_command import handle_command
from server.db import log_event
from server.voice.handsfree import HandsFreeListener, split_wake_word

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
    room_id = identity["room_id"]
    connections.register(client_id, ws)
    log_event(db, "voice", f"Máy '{identity['name']}' đã kết nối")

    buffer = bytearray()
    capturing = False
    busy = False
    handsfree: HandsFreeListener | None = None
    ignore_until = 0.0  # don't listen while our own TTS plays back on this device
    ack_sent = False

    async def maybe_ack() -> None:
        """Phát câu "Đang kiểm tra" cho lệnh chậm — mỗi lệnh một lần."""
        nonlocal ack_sent
        if ack_sent:
            return
        ack_sent = True
        spoken = await pipeline.ack_audio()
        if spoken:
            await connections.send_audio(
                ws, "reply", app.state.cfg.get("voice.ack_text", "Đang kiểm tra"), spoken
            )

    async def handle_utterance(reason: str, pcm: bytes) -> None:
        """Wake-word gate -> command -> spoken reply -> back to listening."""
        nonlocal ignore_until, ack_sent
        ack_sent = False
        result = await pipeline.process_audio(pcm, room_id, skip_gate=True, stt_only=True)
        transcript = result.get("transcript", "")
        if not transcript.strip():  # debug: keep the audio that STT couldn't read
            try:
                import wave

                dbg = app.state.cfg.data_path("debug")
                dbg.mkdir(exist_ok=True)
                with wave.open(str(dbg / f"utt_{int(time.time())}.wav"), "wb") as f:
                    f.setnchannels(1)
                    f.setsampwidth(2)
                    f.setframerate(16000)
                    f.writeframes(pcm)
            except Exception:
                pass
        command = split_wake_word(transcript)
        if command is None:
            await ws.send_json({"type": "ignored", "text": transcript})
            log_event(
                db, "handsfree",
                f"Bỏ qua (thiếu từ đánh thức, {len(pcm)/32000:.1f}s): '{transcript}'",
            )
            await ws.send_json({"type": "listening"})
            return
        await connections.send_audio(ws, "transcript", transcript, None)
        if not command:
            reply = "Dạ, mình nghe nè."
        else:
            task = asyncio.create_task(handle_command(
                db, app.state.drivers, app.state.cfg, app.state.weather, command, room_id,
            ))
            done, _ = await asyncio.wait(
                {task}, timeout=float(app.state.cfg.get("voice.ack_after", 1.2))
            )
            if not done:
                await maybe_ack()
            reply = (await task)["reply"]
        spoken = await pipeline.tts_audio(reply)
        await connections.send_audio(ws, "reply", reply, spoken)
        if spoken:
            data, rate = spoken
            ignore_until = time.monotonic() + len(data) / (rate * 2) + 0.4
        log_event(db, "voice", f"[handsfree/{reason}] '{transcript}' -> {reply}")
        await ws.send_json({"type": "listening"})

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                if busy:
                    continue  # mid-processing: this audio is too late to matter
                if handsfree is not None:
                    if time.monotonic() < ignore_until:
                        continue  # echo guard while our TTS plays on this device
                    done = await handsfree.feed(message["bytes"])
                    if done:
                        reason, pcm = done
                        busy = True
                        try:
                            await handle_utterance(reason, pcm)
                        except Exception as exc:
                            log_event(db, "voice", f"Lỗi hands-free: {exc}", level="warn")
                            await ws.send_json({"type": "listening"})
                        finally:
                            busy = False
                    continue
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
                if handsfree:
                    await handsfree.stop()
                handsfree = None
                buffer.clear()
                capturing = True
                await ws.send_json({"type": "ready"})

            elif msg_type == "handsfree":
                capturing = False
                buffer.clear()
                if handsfree:
                    await handsfree.stop()
                handsfree = HandsFreeListener(pipeline.stt, int(data.get("silence_ms", 3000)))
                await ws.send_json({"type": "listening"})

            elif msg_type == "handsfree_off":
                if handsfree:
                    await handsfree.stop()
                handsfree = None
                await ws.send_json({"type": "ok"})

            elif msg_type == "stop" and not busy:
                capturing = False
                busy = True
                ack_sent = False
                try:
                    audio = bytes(buffer)
                    buffer.clear()
                    if not audio:
                        await ws.send_json({"type": "end"})
                        continue
                    result = await pipeline.process_audio(audio, room_id, on_ack=maybe_ack)
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
        if handsfree:
            await handsfree.stop()
        connections.unregister(client_id)
        log_event(db, "voice", f"Máy '{identity['name']}' ngắt kết nối")
