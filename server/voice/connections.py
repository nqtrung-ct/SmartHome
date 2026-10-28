"""Live WebSocket connections to voice clients (Android phones)."""
from __future__ import annotations

from fastapi import WebSocket

from server.db import Database
from server.util import now_iso


class VoiceConnections:
    def __init__(self, db: Database):
        self.db = db
        self._clients: dict[int, WebSocket] = {}

    def register(self, client_id: int, ws: WebSocket) -> None:
        self._clients[client_id] = ws
        self.db.execute(
            "UPDATE voice_clients SET last_seen=? WHERE id=?",
            (now_iso(), client_id),
        )

    def unregister(self, client_id: int) -> None:
        self._clients.pop(client_id, None)

    async def send_audio(self, ws: WebSocket, kind: str, text: str, audio: tuple[bytes, int] | None) -> None:
        await ws.send_json({"type": kind, "text": text})
        if audio:
            data, rate = audio
            await ws.send_json({"type": "audio", "sample_rate": rate, "format": "wav"})
            for i in range(0, len(data), 8192):
                await ws.send_bytes(data[i : i + 8192])
        await ws.send_json({"type": "end"})

    async def announce_room(self, room_id, text: str, pipeline) -> None:
        """Send a spoken announcement to every client in a room."""
        clients = self.db.query("SELECT id FROM voice_clients WHERE room_id=?", (room_id,))
        ids = [c["id"] for c in clients]
        if not ids:
            from server.db import log_event

            log_event(self.db, "voice", f"Không có máy nào trong phòng để phát: {text}", level="warn")
            return
        audio = None
        if pipeline:
            audio = await pipeline.tts_audio(text)
        for client_id in ids:
            ws = self._clients.get(client_id)
            if ws:
                try:
                    await self.send_audio(ws, "announce", text, audio)
                except Exception:
                    self.unregister(client_id)
