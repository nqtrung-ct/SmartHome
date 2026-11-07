"""So sánh các model LLM (Ollama) cho lệnh tiếng Việt trong nhà thông minh.

Chạy ĐÚNG code path thật (llm_path: prompt hệ thống, TOOLS, vòng tool-calls,
tổng hợp câu trả lời) nhưng thay execute_tool bằng bản mô phỏng — KHÔNG đụng
thiết bị thật, KHÔNG tạo hẹn giờ thật. Kết quả tool giả lập là số liệu thực tế
giả (nhiệt độ 31.5°C, pin 82%...) để đo chất lượng câu tổng hợp của model.

Chạy:
    .venv\\Scripts\\python.exe -X utf8 scripts\\llm_eval.py --models qwen3:8b qwen2.5:7b-instruct
    .venv\\Scripts\\python.exe -X utf8 scripts\\llm_eval.py --models qwen3:8b --room khach

Chi tiết từng câu (tools gọi + args + câu trả lời tổng hợp) ghi ra
data/llm_eval_results.json để soi chất lượng câu tổng hợp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.config import Config
from server.core import text_command
from server.core.text_command import llm_path
from server.db import Database
from server.util import now, normalize

# (câu nói, tools kỳ vọng [thứ tự tự do], args kỳ vọng {tool.key: giá trị})
# args kiểm tra dạng "tồn tại ít nhất 1 call có key khớp giá trị".
CASES = [
    ("tắt đèn", ["switch"], {"switch.state": "off"}),
    ("bật quạt lên", ["switch"], {"switch.state": "on"}),
    ("bật quạt phòng con", ["switch"], {"switch.state": "on"}),
    ("tắt hết đèn phòng khách", ["switch"], {"switch.state": "off"}),
    ("mở máy lạnh", ["switch|set_climate"],
     {"switch.state|set_climate.mode": "on"}),  # 1 trong 2 tool
    ("điều hòa 25 độ", ["set_climate"], {"set_climate.temperature": 25}),
    ("bật điều hòa chế độ sưởi 26 độ", ["set_climate"],
     {"set_climate.mode": "heat", "set_climate.temperature": 26}),
    ("tắt điều hòa rồi cho tôi biết thời tiết hôm nay",
     ["switch|set_climate", "get_weather"], {"switch.state|set_climate.mode": "off"}),
    ("phòng khách bao nhiêu độ", ["get_sensor"], {"get_sensor.metric": "temperature"}),
    ("độ ẩm bây giờ bao nhiêu phần trăm", ["get_sensor"], {"get_sensor.metric": "humidity"}),
    ("hôm nay thời tiết thế nào", ["get_weather"], {"get_weather.day": "today"}),
    ("tin tức hôm nay có gì mới", ["get_news"], {}),
    ("tin giải trí mới nhất hôm nay", ["get_news"], {"get_news.topic": "giải trí"}),
    ("có tin thế giới gì mới không", ["get_news"], {"get_news.topic": "thế giới"}),
    ("đọc giúp tôi tin về nữ tỷ phú lừa đảo", ["read_article"],
     {"read_article.query": "tỷ phú"}),
    ("ngày mai có mưa không", ["get_weather"], {"get_weather.day": "tomorrow"}),
    ("mấy giờ rồi", ["get_datetime"], {"get_datetime.part": "time"}),
    ("hôm nay thứ mấy rồi", ["get_datetime"], {"get_datetime.part": "date"}),
    ("âm lịch hôm nay là ngày gì", ["get_datetime"], {"get_datetime.part": "lunar"}),
    ("pin mặt trời còn bao nhiêu phần trăm", ["get_solar"], {"get_solar.metric": "battery"}),
    ("điện mặt trời đang sản xuất bao nhiêu", ["get_solar"], {"get_solar.metric": "pv"}),
    ("cái gì đang bật trong nhà", ["list_active"], {}),
    ("hẹn 15 phút nữa nhắc tôi tắt bếp", ["set_timer"],
     {"set_timer.minutes": 15}),
    ("mỗi ngày 6 giờ sáng bật quạt", ["set_timer"],
     {"set_timer.time": "06:00", "set_timer.repeat": "daily"}),
    ("hẹn giờ nào đang chờ", ["list_timers"], {}),
    ("quay camera về góc bếp", ["camera_preset"], {"camera_preset.preset": "bep"}),
    ("kiểm tra camera có người không", ["camera_person"], {}),
    ("chào buổi sáng", [], {}),          # KHÔNG gọi tool
    ("cảm ơn nhé", [], {}),
    ("ừm", [], {}),
]

# Kết quả giả lập cho từng tool — số liệu thật giả, tiếng Việt, như act_* thật.
def sim_reply(name: str, args: dict) -> str:
    a = args or {}
    if name == "switch":
        verb = "bật" if a.get("state") == "on" else "tắt"
        room = f" phòng {a['room']}" if a.get("room") else ""
        return f"Đã {verb} {a.get('entity') or 'thiết bị'}{room}."
    if name == "set_climate":
        bits = [f"mức {a['temperature']} độ" if a.get("temperature") is not None else None,
                f"chế độ {a['mode']}" if a.get("mode") else None]
        return "Điều hòa đã chỉnh " + ", ".join(b for b in bits if b) + "." \
            if any(bits) else "Điều hòa đã bật."
    if name == "press_button":
        return f"Đã bấm {a.get('button')} trên {a.get('entity') or 'thiết bị'}."
    if name == "get_weather":
        when = {"today": "Hôm nay", "tomorrow": "Ngày mai",
                "day_after": "Ngày mốt"}.get(a.get("day", "today"), "Hôm nay")
        return f"{when} Cần Thơ trời nắng nóng 33°C, chiều tối có mưa rào rải rác, khả năng mưa 40%."
    if name == "get_news":
        head = {"giai tri": "Giải trí", "the gioi": "Thế giới"}.get(
            a.get("topic", ""), "Tin mới nhất")
        return (f"{head} hiện có: (VNExpress) Thủ tướng Thái Lan trấn an người dân; "
                "(Tuổi Trẻ) IMAX báo lãi tăng 400% nhờ bom tấn mới; "
                "(Dân Trí) Miền Bắc mưa to do không khí lạnh.")
    if name == "read_article":
        return ("Bài 'Phim về nữ tỷ phú lừa đảo', nội dung: Phim kể về Elizabeth "
                "Holmes sau khi được tại ngoại. Tác phẩm dẫn đầu phòng vé Bắc Mỹ "
                "thu 11,4 triệu USD, tổng doanh thu 64,7 triệu USD. Giới phê bình "
                "khen cách kể của đạo diễn Nathan Fielder.")
    if name == "get_sensor":
        return "Độ ẩm trong phòng 68%." if a.get("metric") == "humidity" \
            else "Nhiệt độ trong phòng 31.5°C."
    if name == "get_datetime":
        t = now()
        if a.get("part") == "time":
            return f"Bây giờ là {t:%H:%M}."
        if a.get("part") == "lunar":
            return f"Hôm nay {t:%d/%m/%Y}, âm lịch mùng 3 tháng 9."
        thu = ["hai", "ba", "tư", "năm", "sáu", "bảy", "chủ nhật"][t.weekday()]
        return f"Hôm nay là thứ {thu}, ngày {t:%d/%m/%Y}."
    if name == "get_solar":
        return {"battery": "Pin đang 82% và đang xả.",
                "pv": "Điện mặt trời đang sản xuất 1.850 W.",
                "load": "Nhà đang dùng 620 W."}.get(a.get("metric", ""),
                "Pin 82%, điện mặt trời 1.850 W, nhà đang dùng 620 W.")
    if name == "list_active":
        return "Đang bật: đèn trần phòng khách, quạt trần phòng khách, tivi phòng khách."
    if name == "list_timers":
        return "Đang chờ 2 hẹn giờ: 17:00 tưới cây (mỗi ngày), 21:30 tắt hết đèn."
    if name == "set_timer":
        when = f"sau {a['minutes']} phút" if a.get("minutes") is not None \
            else f"lúc {a.get('time')}" if a.get("time") else ""
        rep = f", lặp lại {a['repeat']}" if a.get("repeat") and a["repeat"] != "none" else ""
        what = a.get("message") or f"{a.get('state')} {a.get('entity') or ''}".strip() or "nhắc"
        return f"Đã hẹn {when}: {what}{rep}."
    if name == "cancel_timer":
        return "Đã hủy hẹn giờ."
    if name == "move_timer":
        return "Đã dời hẹn giờ."
    if name == "camera_preset":
        return f"Camera đã quay về góc {a.get('preset', '')}."
    if name == "camera_person":
        return "Không thấy người trong khung hình."
    return f"Đã chạy {name}."


CALLS: list[dict] = []


async def sim_execute_tool(db, drivers, weather, name, args, room_id, day_hint=0,
                           source_text=""):
    CALLS.append({"tool": name, "args": args or {}})
    return {"reply": sim_reply(name, args), "action": {"tool": name, "ok": True}}


class CfgOverride:
    """Config view đè vài khóa (đổi model cho từng lượt chạy)."""
    def __init__(self, cfg, over: dict):
        self._cfg, self._over = cfg, over

    def get(self, key, default=None):
        return self._over[key] if key in self._over else self._cfg.get(key, default)

    def __getattr__(self, k):
        return getattr(self._cfg, k)


def arg_ok(calls, key: str, want) -> bool:
    """key dạng 'tool.arg' (nhiều phương án cách nhau '|'); giá trị khớp
    bằng/số/chuỗi-normalize-chứa; giờ HH:MM so không phân biệt số 0 đầu."""
    for alt in key.split("|"):
        tool, field = alt.split(".", 1)
        for c in calls:
            if c["tool"] != tool or field not in c["args"]:
                continue
            got = c["args"][field]
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                if abs(float(got) - float(want)) < 1e-6:
                    return True
            elif isinstance(got, str):
                g, w = normalize(got), normalize(str(want))
                if w in g:
                    return True
                if re.fullmatch(r"\d{1,2}:\d{2}", w) and re.fullmatch(r"\d{1,2}:\d{2}", g):
                    if [int(x) for x in w.split(":")] == [int(x) for x in g.split(":")]:
                        return True
    return False


def pick_room(db_path: Path, hint: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute("SELECT id, name FROM rooms ORDER BY id").fetchall()
    con.close()
    for rid, name in rows:
        if hint in normalize(name or ""):
            return rid, name
    return (rows[0][0], rows[0][1]) if rows else (None, None)


async def run_model(model: str, cfg, db, room_id) -> list[dict]:
    m_cfg = CfgOverride(cfg, {"llm.model": model})
    out = []
    for text, want_tools, want_args in CASES:
        CALLS.clear()
        t0 = time.perf_counter()
        err = ""
        try:
            res = await llm_path(db, None, m_cfg, None, text, room_id)
            reply = res["reply"]
        except Exception as exc:  # net an toàn gọi weather=None khi model bỏ sót tool
            reply, err = f"<lỗi: {exc}>", "net-error"
        dt = time.perf_counter() - t0
        names = [c["tool"] for c in CALLS]
        tools_ok = all(any(w in names for w in wt.split("|")) for wt in want_tools)
        if not want_tools:
            tools_ok = not names
        args_ok = all(arg_ok(CALLS, k, v) for k, v in want_args.items())
        out.append({"text": text, "tools": names, "calls": list(CALLS),
                    "tools_ok": tools_ok, "args_ok": args_ok,
                    "secs": round(dt, 2), "reply": reply, "err": err})
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["qwen3:8b", "qwen3.5:4b"])
    ap.add_argument("--room", default="khach", help="từ khóa tên phòng đứng theo mic")
    args = ap.parse_args()

    cfg = Config()
    db = Database(cfg.data_path("smarthome.db"))
    room_id, room_name = pick_room(cfg.data_path("smarthome.db"), args.room)
    print(f"Phòng đứng theo mic: {room_name!r} (id={room_id})")

    text_command.execute_tool = sim_execute_tool  # mô phỏng — không side effect

    all_results = {}
    for model in args.models:
        print(f"\n=== {model} ===")
        rows = await run_model(model, cfg, db, room_id)
        all_results[model] = rows
        t_ok = sum(r["tools_ok"] for r in rows)
        a_ok = sum(r["tools_ok"] and r["args_ok"] for r in rows)
        secs = [r["secs"] for r in rows]
        print(f"Chọn tool đúng: {t_ok}/{len(rows)} | thêm args đúng: {a_ok}/{len(rows)} | "
              f"giây: trung bình {sum(secs)/len(secs):.1f}, chậm nhất {max(secs):.1f}")
        for r in rows:
            if not r["tools_ok"] or not r["args_ok"] or r["err"]:
                print(f"  ✗ {r['text']!r} -> gọi {r['tools']}"
                      + (f" args={r['calls']}" if not r["args_ok"] else "")
                      + (f" [{r['err']}]" if r["err"] else ""))

    out_path = Path("data/llm_eval_results.json")
    out_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\nChi tiết đầy đủ (kể cả câu tổng hợp): {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
