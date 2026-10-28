"""Smoke test: run against a live server.

  python -m venv .venv && .venv\\Scripts\\activate
  pip install httpx
  python scripts/smoke_test.py http://127.0.0.1:8124 <admin_token>
"""
from __future__ import annotations

import asyncio
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

passed, failed = 0, 0


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=30) as c:
        r = await c.get("/api/health")
        check("health", r.status_code == 200)

        # bad token rejected
        r = await c.get("/api/rooms", headers={"Authorization": "Bearer nope"})
        check("auth rejects bad token", r.status_code == 401)

        # demo data
        r = await c.post("/api/demo")
        check("seed demo", r.status_code == 201, r.text)
        demo_device_id = r.json().get("device_id")

        rooms = (await c.get("/api/rooms")).json()
        check("rooms exist", any(x["name"] == "Phòng khách" for x in rooms), str(rooms))
        living = next(x for x in rooms if x["name"] == "Phòng khách")

        devices = (await c.get("/api/devices")).json()
        demo = next((d for d in devices if d["id"] == demo_device_id), None)
        check("demo device has 4 entities", demo and len(demo["entities"]) == 4)

        light = next(e for e in demo["entities"] if e["entity_type"] == "light")
        r = await c.post(f"/api/entities/{light['id']}/command", json={"command": "on"})
        check("light on", r.status_code == 200 and r.json()["state"]["state"] == "on", r.text)

        # --- text commands: fast path -------------------------------------
        async def say(text, room=living["id"]):
            return (await c.post("/api/command", json={"text": text, "room_id": room})).json()

        res = await say("bật đèn")
        check("fast: bật đèn", "đã bật đèn" in res["reply"].lower() and res["engine"] == "fast", str(res))
        res = await say("tắt đèn phòng khách")
        check("fast: tắt đèn + phòng", "đã tắt" in res["reply"].lower() and res["engine"] == "fast", str(res))
        res = await say("bật điều hòa 26 độ")
        check("fast: điều hòa 26 độ", "26" in res["reply"] and "điều hòa" in res["reply"].lower(), str(res))
        res = await say("mười lăm phút nữa nhắc tôi tắt bếp")
        check("fast: timer 15 phút", "15" in res["reply"] or ":" in res["reply"], str(res))
        timers = (await c.get("/api/timers")).json()
        check("timer stored", len(timers["pending"]) >= 1, str(timers))

        res = await say("nhiệt độ trong phòng thế nào")
        check("fast: sensor", "độ" in res["reply"], str(res))

        # weather needs internet; tolerate failure
        try:
            res = await say("thời tiết hôm nay thế nào")
            check("weather", "độ" in res["reply"] or "thời tiết" in res["reply"].lower(), str(res))
        except Exception as exc:
            print(f"[SKIP] weather (no internet?): {exc}")

        # pairing flow
        code = (await c.post("/api/pairing-codes")).json()["code"]
        r = await c.post("/api/pair", json={"code": code, "client_name": "smoke-test"},
                         headers={})
        check("pair phone", r.status_code == 201, r.text)
        client = r.json()
        r = await c.get("/api/me", params={"token": client["token"]})
        check("client /me", r.status_code == 200, r.text)

        r = await c.patch(f"/api/voice-clients/{client['client_id']}",
                          json={"room_id": living["id"]})
        check("assign client to room", r.status_code == 200, r.text)

        # events
        r = await c.get("/api/events")
        check("events logged", len(r.json()) > 3)

        print(f"\n{passed} passed, {failed} failed")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
