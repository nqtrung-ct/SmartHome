"""Quick WebSocket voice-gateway check (server must be running).

  python scripts/ws_voice_test.py http://127.0.0.1:8124 <admin_token>

Pairs a fake client, connects to /ws/voice, streams 1s of silence and prints
every message the server sends back.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "changeme"


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        code = (await c.post("/api/pairing-codes", headers={"Authorization": f"Bearer {TOKEN}"})).json()["code"]
        client = (await c.post("/api/pair", json={"code": code, "client_name": "ws-test"})).json()

    ws_url = BASE.replace("http", "ws") + "/ws/voice?token=" + client["token"]
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "start"}))
        print(">", await ws.recv())  # ready

        await ws.send(b"\x00\x00" * 16000)  # 1s of silence
        await ws.send(json.dumps({"type": "stop"}))

        saw_reply = False
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(msg, bytes):
                print(f"<binary {len(msg)} bytes>")
                continue
            print("<", msg)
            mtype = json.loads(msg).get("type")
            if mtype == "reply":
                saw_reply = True
            elif mtype == "end" and saw_reply:
                break

    print("\nWS voice gateway OK")


if __name__ == "__main__":
    asyncio.run(main())
