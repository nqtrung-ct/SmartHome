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
