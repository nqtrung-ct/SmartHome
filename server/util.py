"""Small shared helpers: text normalization, Vietnamese number words, time."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta


def normalize(text: str) -> str:
    """Lowercase, strip Vietnamese diacritics, collapse whitespace.

    Matching commands against entity aliases happens on normalized text,
    so "Bật ĐÈN" and "bat den" both hit the alias "đèn".
    """
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.replace("đ", "d")  # d-with-stroke does not decompose under NFD
    return re.sub(r"\s+", " ", text).strip()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def lan_ip() -> str | None:
    """LAN IPv4 of the interface holding the default route (UDP trick, no traffic sent).

    Machines with VirtualBox/WSL/VPN adapters have several candidates; this picks
    the one that actually reaches the internet, which is the home LAN.
    """
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local = s.getsockname()[0]
        s.close()
        return local
    except OSError:
        return None


def store_secret(cfg, group: str, key: str, value: str) -> None:
    """Persist device secrets (Tuya keys, RTSP passwords) into data/secrets.json."""
    import json

    path = cfg.data_path("secrets.json")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data.setdefault(group, {})[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_secret(cfg, group: str, key: str) -> str | None:
    """Read a value stored by store_secret, or None."""
    import json

    path = cfg.data_path("secrets.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(group, {}).get(key)
    except Exception:
        return None


def set_yaml_value(path, section: str, key: str, value) -> None:
    """Set section.key in a YAML file, preserving comments and other lines."""
    import json
    import re
    from pathlib import Path

    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    cur = None
    section_seen = False
    insert_at = None
    replaced = False
    entry = f"  {key}: {json.dumps(value, ensure_ascii=False)}"
    for line in lines:
        header = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if header:
            if cur == section:
                insert_at = len(out)  # boundary: the next section starts here
            cur = header.group(1)
            if cur == section:
                section_seen = True
        if cur == section and not replaced:
            mkey = re.match(rf"^(\s+{re.escape(key)}\s*:\s*)(.*?)(\s*#.*)?$", line)
            if mkey:
                # keep the line's trailing comment, replace just the value
                out.append(f"{mkey.group(1)}{json.dumps(value, ensure_ascii=False)}{mkey.group(3) or ''}")
                replaced = True
                continue
        out.append(line)
    if not replaced:
        if not section_seen:
            out += [f"{section}:", entry]
        elif insert_at is not None:
            out.insert(insert_at, entry)
        else:
            out.append(entry)  # section is the last one in the file
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# Vietnamese number words (normalized, no diacritics), enough for timers/temps.
# Handles 0-999: "muoi lam"=15, "hai muoi mot"=21, "mot tram hai muoi"=120.

_DIGITS = {
    "khong": 0, "mot": 1, "hai": 2, "ba": 3, "bon": 4,
    "nam": 5, "sau": 6, "bay": 7, "tam": 8, "chin": 9,
}
_NUM_WORDS = set(_DIGITS) | {"muoi", "tram", "lam"}


def vn_number(tokens: list[str]) -> float | None:
    total, cur, seen = 0, 0, False
    for token in tokens:
        if token in _DIGITS:
            cur = cur * 10 + _DIGITS[token]
            seen = True
        elif token == "muoi":
            cur = (cur if cur else 1) * 10
            seen = True
        elif token == "tram":
            total += (cur if cur else 1) * 100
            cur = 0
            seen = True
        elif token == "lam":  # "mười lăm" -> 15
            if cur >= 10:
                cur += 5
                seen = True
            else:
                return None
        else:
            return None
    return float(total + cur) if seen else None


def trailing_number(tokens: list[str]) -> tuple[float, int] | None:
    """Read the number that ends at the last token: return (value, start_index).

    Used like: tokens before "phut" in "muoi lam phut nua nhac..." -> 15.
    """
    i = len(tokens)
    while i > 0:
        token = tokens[i - 1]
        if token.isdigit() or token in _NUM_WORDS:
            i -= 1
        else:
            break
    part = tokens[i:]
    if not part:
        return None
    if len(part) == 1 and part[0].isdigit():
        return float(part[0]), i
    value = vn_number(part)
    return (value, i) if value is not None else None


def next_hhmm(hour: int, minute: int) -> datetime:
    """The next wall-clock occurrence of hour:minute (today, or tomorrow if passed)."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def hhmm_adjusted(hour: int, minute: int, part_of_day: str | None) -> int:
    """Apply 'chieu/toi/dem' (+12h) and 'trua' semantics to a 12h-style hour."""
    if part_of_day in ("chieu", "toi", "dem") and 1 <= hour < 12:
        return hour + 12
    return hour
