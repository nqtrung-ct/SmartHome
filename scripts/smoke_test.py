"""Smoke test against a live server. Builds its own fixtures via the normal API
and cleans them up afterwards - no demo data left behind.

  python scripts/smoke_test.py http://127.0.0.1:8124 <admin_token>
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8124"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else "changeme"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

passed, failed = 0, 0
created = {"rooms": [], "devices": [], "clients": []}


def check(name: str, ok: bool, detail: str = ""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


async def cleanup(c):
    for did in created["devices"]:
        await c.delete(f"/api/devices/{did}")
    for rid in created["rooms"]:
        await c.delete(f"/api/rooms/{rid}")
    for cid in created["clients"]:
        await c.delete(f"/api/voice-clients/{cid}")


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=30) as c:
        try:
            r = await c.get("/api/health")
            check("health", r.status_code == 200)

            r = await c.get("/api/rooms", headers={"Authorization": "Bearer nope"})
            check("auth rejects bad token", r.status_code == 401)

            # --- rooms with floors ------------------------------------------
            r = await c.post("/api/rooms", json={"name": "SH Test Phòng khách", "floor": "Trệt", "aliases": "khách,khach"})
            check("create room with floor", r.status_code == 201, r.text)
            living = r.json()["id"]
            created["rooms"].append(living)
            r = await c.post("/api/rooms", json={"name": "SH Test Phòng ngủ", "floor": "Tầng 1"})
            bedroom = r.json()["id"]
            created["rooms"].append(bedroom)

            rooms = (await c.get("/api/rooms")).json()
            r1 = next(x for x in rooms if x["id"] == living)
            check("room floor saved", r1.get("floor") == "Trệt", str(r1))
            r = await c.patch(f"/api/rooms/{bedroom}", json={"floor": "Tầng 2"})
            check("room floor patched", r.status_code == 200, r.text)

            # --- floor CRUD ---------------------------------------------------
            floors = (await c.get("/api/floors")).json()
            check("floors list seeded", any(f["name"] == "Trệt" for f in floors), str(floors))
            r = await c.post("/api/floors", json={"name": "SH Tầng test"})
            check("floor create", r.status_code == 201, r.text)
            test_floor_id = r.json()["id"]
            r = await c.post("/api/floors", json={"name": "SH Tầng test"})
            check("floor duplicate rejected", r.status_code == 400, r.text)
            r = await c.patch(f"/api/rooms/{bedroom}", json={"floor": "SH Tầng test"})
            check("room moved to floor", r.status_code == 200, r.text)
            r = await c.patch(f"/api/floors/{test_floor_id}", json={"name": "SH Tầng đổi tên"})
            check("floor rename", r.status_code == 200, r.text)
            rooms = (await c.get("/api/rooms")).json()
            r2 = next(x for x in rooms if x["id"] == bedroom)
            check("floor rename cascades to rooms", r2.get("floor") == "SH Tầng đổi tên", str(r2))
            r = await c.delete(f"/api/floors/{test_floor_id}")
            check("floor delete", r.status_code == 200, r.text)
            rooms = (await c.get("/api/rooms")).json()
            r2 = next(x for x in rooms if x["id"] == bedroom)
            check("floor delete clears room floor", r2.get("floor") == "", str(r2))

            # --- virtual device via the normal API (manual add path) --------
            r = await c.post("/api/devices", json={
                "name": "SH Test thiết bị", "driver": "dummy", "room_id": living})
            check("create manual device", r.status_code == 201, r.text)
            device_id = r.json()["id"]
            created["devices"].append(device_id)

            entity_ids = []
            for name, etype, aliases in [
                ("Đèn trần", "light", "đèn,đèn trần"),
                ("Quạt", "switch", "quạt"),
                ("Điều hòa", "climate", "điều hòa,máy lạnh,ac"),
                ("Cảm biến", "sensor", "cảm biến,nhiệt độ,độ ẩm"),
            ]:
                r = await c.post(f"/api/devices/{device_id}/entities",
                                 json={"name": name, "entity_type": etype, "aliases": aliases})
                check(f"create entity {name}", r.status_code == 201, r.text)
                entity_ids.append(r.json()["id"])

            light_id, fan_id, climate_id, sensor_id = entity_ids
            r = await c.post(f"/api/entities/{light_id}/command", json={"command": "on"})
            check("light on", r.status_code == 200 and r.json()["state"]["state"] == "on", r.text)

            # --- text commands: fast path -------------------------------------
            async def say(text, room=living):
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

            try:
                res = await say("thời tiết hôm nay thế nào")
                check("weather", "độ" in res["reply"] or "thời tiết" in res["reply"].lower(), str(res))
            except Exception as exc:
                print(f"[SKIP] weather (no internet?): {exc}")

            # --- manual add with rtsp secret ----------------------------------
            r = await c.post("/api/devices", json={
                "name": "SH Test camera", "driver": "onvif", "host": "192.0.2.123",
                "extra": {"rtsp_password": "test-rtsp-pass"}})
            check("manual camera add", r.status_code == 201, r.text)
            created["devices"].append(r.json()["id"])

            # --- assistant tab endpoints ---------------------------------------
            r = await c.get("/api/assistant/status")
            st = r.json() if r.status_code == 200 else {}
            check("assistant status", r.status_code == 200 and all(k in st for k in ("llm", "stt", "tts")), r.text)
            if st.get("tts", {}).get("engine") == "none":
                r = await c.post("/api/assistant/test-tts", json={"text": "test"})
                check("tts test disabled when engine none", r.status_code == 400, r.text)
            r = await c.post("/api/assistant/announce", json={"room_id": living, "text": "test"})
            check("announce with no clients", r.status_code == 200 and r.json()["sent"] == 0, r.text)

            # --- pairing flow --------------------------------------------------
            code = (await c.post("/api/pairing-codes")).json()["code"]
            r = await c.post("/api/pair", json={"code": code, "client_name": "smoke-test"})
            check("pair phone", r.status_code == 201, r.text)
            client = r.json()
            created["clients"].append(client["client_id"])
            r = await c.get("/api/me", params={"token": client["token"]})
            check("client /me", r.status_code == 200, r.text)
            r = await c.patch(f"/api/voice-clients/{client['client_id']}", json={"room_id": living})
            check("assign client to room", r.status_code == 200, r.text)

            r = await c.get("/api/events")
            check("events logged", len(r.json()) > 3)

        finally:
            await cleanup(c)

        # cleanup worked?
        rooms = (await c.get("/api/rooms")).json()
        check("cleanup removed test data",
              not any(x["name"].startswith("SH Test") for x in rooms), str(rooms))

        print(f"\n{passed} passed, {failed} failed")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
