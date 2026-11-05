"""Tin tức qua RSS cho tool get_news: VNExpress, Tuổi Trẻ, Dân Trí.

Mỗi topic = 3 feed (mỗi báo một feed), fetch đồng thời, cache 10 phút để
câu hỏi lặp lại không đập RSS. Trả về danh sách tiêu đề đã dọn cho LLM
tổng hợp — model KHÔNG tự biết tin tức (offline), mọi dữ liệu đều từ đây.
"""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET

import httpx

# topic (không dấu, khớp sau normalize) -> feed URLs
FEEDS = {
    "moi nhat": [
        "https://vnexpress.net/rss/tin-moi-nhat.rss",
        "https://tuoitre.vn/rss/tin-moi-nhat.rss",
        "https://dantri.com.vn/rss/home.rss",
    ],
    "thoi su": [  # "chính trị" trong nước — Dân Trí không có feed riêng
        "https://vnexpress.net/rss/thoi-su.rss",
        "https://tuoitre.vn/rss/thoi-su.rss",
        "https://dantri.com.vn/rss/xa-hoi.rss",
    ],
    "the gioi": [
        "https://vnexpress.net/rss/the-gioi.rss",
        "https://tuoitre.vn/rss/the-gioi.rss",
        "https://dantri.com.vn/rss/the-gioi.rss",
    ],
    "kinh doanh": [
        "https://vnexpress.net/rss/kinh-doanh.rss",
        "https://tuoitre.vn/rss/kinh-doanh.rss",
        "https://dantri.com.vn/rss/kinh-doanh.rss",
    ],
    "cong nghe": [
        "https://vnexpress.net/rss/cong-nghe.rss",
        "https://tuoitre.vn/rss/cong-nghe.rss",
        "https://dantri.com.vn/rss/cong-nghe.rss",
    ],
    "the thao": [
        "https://vnexpress.net/rss/the-thao.rss",
        "https://tuoitre.vn/rss/the-thao.rss",
        "https://dantri.com.vn/rss/the-thao.rss",
    ],
    "giai tri": [
        "https://vnexpress.net/rss/giai-tri.rss",
        "https://tuoitre.vn/rss/giai-tri.rss",
        "https://dantri.com.vn/rss/giai-tri.rss",
    ],
}

# cách gọi khác của topic hay gặp
ALIASES = {"chinh tri": "thoi su", "tin tuc": "moi nhat", "tin moi": "moi nhat",
           "trong nuoc": "thoi su", "quoc te": "the gioi", "gia dinh": "giai tri"}

TOPIC_LABEL = {"moi nhat": "tin mới nhất", "thoi su": "thời sự",
               "the gioi": "thế giới", "kinh doanh": "kinh doanh",
               "cong nghe": "công nghệ", "the thao": "thể thao",
               "giai tri": "giải trí"}

PER_SOURCE = 3        # tiêu đề mỗi báo
CACHE_TTL = 600       # 10 phút
_TIMEOUT = httpx.Timeout(8.0)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) SmartHome/1.0")

_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}  # topic -> (ts, items)
# tên hiển thị nguồn theo hostname (thứ tự import không quan trọng)
_SOURCE_NAME = {"vnexpress.net": "VNExpress", "tuoitre.vn": "Tuổi Trẻ",
                "dantri.com.vn": "Dân Trí"}

_TAIL = re.compile(r"\s*[-–|]\s*(VnExpress|VNExpress|Tuổi Trẻ|Tuoitre|Dân Trí|Dantri)\s*$")


def _parse_items(content: bytes, source: str) -> list[tuple[str, str, str, str]]:
    """[(tiêu đề, tên nguồn, link, sapo)] — lỗi parse thì trả rỗng, không đổ cả cụm."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        title = _TAIL.sub("", title).strip()
        # bỏ thẻ HTML lẫn trong tiêu đề nếu có
        title = re.sub(r"<[^>]+>", "", title).strip()
        link = (item.findtext("link") or "").strip()
        sapo = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
        if title and link:
            out.append((title, source, link, sapo))
    return out


async def _fetch_one(client: httpx.AsyncClient, url: str) -> list[tuple[str, str, str, str]]:
    host = re.sub(r"^www\.", "", url.split("/")[2])
    source = _SOURCE_NAME.get(host, host)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return _parse_items(resp.content, source)[:PER_SOURCE]
    except Exception:
        return []  # một báo chết không được ăn mất hai báo còn lại


async def fetch_headlines(topic: str) -> list[tuple[str, str, str, str]]:
    """Tiêu đề của topic (đã normalize không dấu). Cache CACHE_TTL giây."""
    feeds = FEEDS.get(topic) or FEEDS["moi nhat"]
    hit = _cache.get(topic)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        rows = await asyncio.gather(*(_fetch_one(client, u) for u in feeds))
    # xen kẽ từng báo cho đỡ lệch nguồn: VNE1, TT1, DT1, VNE2...
    items: list[tuple[str, str, str, str]] = []
    for i in range(PER_SOURCE):
        for r in rows:
            if i < len(r):
                items.append(r[i])
    _cache[topic] = (time.monotonic(), items)
    return items


async def act_news(topic: str = "moi nhat") -> dict:
    """Kết quả tool get_news: reply = bản tóm tắt tiêu đề cho LLM tổng hợp."""
    topic = ALIASES.get(topic, topic)
    if topic not in FEEDS:
        topic = "moi nhat"
    items = await fetch_headlines(topic)
    if not items:
        return {"reply": "Không lấy được tin tức lúc này (mất mạng hoặc nguồn lỗi).",
                "action": {"tool": "get_news", "ok": False}}
    lines = "; ".join(f"({src}) {title}" for title, src, _, _ in items)
    return {"reply": f"{TOPIC_LABEL[topic].capitalize()} hiện có: {lines}.",
            "action": {"tool": "get_news", "ok": True, "topic": topic,
                       "count": len(items)}}


# --- đọc một tin cụ thể ------------------------------------------------------

_P_BLOCK = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_MAX_INPUT_WORDS = 800    # input cho LLM tóm tắt — 800 từ VN ≈ 1.500 token,
                           # cộng system+TOOLS ≈ 1.600 token vẫn nằm trong num_ctx 4096
                           # (đã trả giá: để 1.200 từ là tràn context, Ollama cắt
                           # mất system prompt, model nhả rác JSON)
_MAX_PARAS = 25           # thân bài thường <= 25 đoạn; sau đó là widget/related
# mẫu chuỗi UI/quảng cáo hay lẫn trong <p> của trang báo (đã trả giá:
# model tóm tắt nhàm widget "gửi thành công" của VnExpress)
_JUNK = re.compile(
    r"(gui thanh cong|dau tich xanh|dang nhap|dang ky|binh luan|quang cao|"
    r"chia se|tu khoa|theo doi|newsletter|tai app|doc nhieu|ban doc|"
    r"co the ban quan tam|xem them|goi y|dang ky ngay|don doc|doc chi tiet)", re.I)


def _clean_paras(page: str) -> list[str]:
    """Các đoạn văn thật từ HTML: đủ dài, không phải caption/quiz/widget."""
    from server.util import normalize

    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.I | re.S)
    from html import unescape

    paras = []
    for m in _P_BLOCK.finditer(page):
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = unescape(re.sub(r"\s+", " ", text)).strip()
        # junk matching trên bản normalize (bỏ dấu) — _JUNK viết không dấu
        if len(text) < 60 or _JUNK.search(normalize(text)):
            continue
        paras.append(text)
        if len(paras) >= _MAX_PARAS:
            break
    return paras


async def _fetch_article_text(url: str, sapo: str = "") -> str:
    """Thân bài: sapo từ RSS (sạch chắc) + các đoạn <p> đã lọc."""
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA},
                                 follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        page = resp.text
    paras = _clean_paras(page)
    # VNExpress: description RSS == đúng đoạn đầu thân bài — bỏ 1 bản
    if sapo and paras:
        from server.util import normalize as _norm

        if _norm(paras[0])[:60] == _norm(re.sub(r"<[^>]+>", " ", sapo))[:60]:
            paras = paras[1:]
    parts = ([re.sub(r"<[^>]+>", " ", sapo).strip()] if sapo else []) + paras
    return " ".join(p for p in parts if p)


def _match_item(query: str, items: list[tuple[str, str, str, str]]):
    """Tin khớp query: chứa nguyên cụm, rồi lỏng dần theo từng từ."""
    from server.util import normalize as _norm

    q = _norm(query)
    if not q:
        return None
    hit = next((it for it in items if q in _norm(it[0])), None)
    if hit:
        return hit
    words = [w for w in q.split() if len(w) > 2]
    if words:
        hit = next((it for it in items
                    if all(w in _norm(it[0]) for w in words)), None)
    return hit


async def act_article(query: str) -> dict:
    """Tool read_article: lấy thân bài tin khớp query, trả text thưa cho LLM
    tóm tắt tối đa 250 từ (giới hạn nằm ở system prompt)."""
    pool: list[tuple[str, str, str, str]] = [it for _, cached in _cache.values() for it in cached]
    if not pool:
        pool = await fetch_headlines("moi nhat")
    hit = _match_item(query, pool)
    if hit is None:
        titles = "; ".join(t for t, _, _, _ in pool[:6])
        return {"reply": f"Không tìm thấy tin khớp '{query}'. Các tin đang có: {titles}.",
                "action": {"tool": "read_article", "ok": False}}
    title, source, link, sapo = hit
    try:
        body = await _fetch_article_text(link, sapo)
    except Exception:
        return {"reply": f"Không mở được bài viết từ {source} lúc này.",
                "action": {"tool": "read_article", "ok": False, "title": title}}
    if not body:
        return {"reply": f"Bài '{title}' ({source}) không đọc được nội dung.",
                "action": {"tool": "read_article", "ok": False, "title": title}}
    body = " ".join(body.split()[:_MAX_INPUT_WORDS])
    return {"reply": f"Bài '{title}' ({source}), nội dung: {body}",
            "action": {"tool": "read_article", "ok": True, "title": title,
                       "source": source, "url": link}}
