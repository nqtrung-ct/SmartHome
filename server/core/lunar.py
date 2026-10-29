"""Âm lịch Việt Nam (múi giờ +7): đổi ngày dương -> ngày âm, tên năm can chi.

Dùng package `lunarcalendar` (bảng lịch 1900-2100 của bản chuyển đổi
Lunar-Solar-Calendar-Converter, cố định múi giờ Việt Nam). Nhớ thêm
`lunarcalendar` vào requirements.txt khi dựng máy mới.
"""
from __future__ import annotations

from lunarcalendar import Converter, Solar


def solar_to_lunar(dd: int, mm: int, yy: int) -> tuple[int, int, int, bool]:
    """(ngày, tháng, năm âm, tháng-nhuận?) của một ngày dương lịch."""
    lu = Converter.Solar2Lunar(Solar(yy, mm, dd))
    return lu.day, lu.month, lu.year, bool(lu.isleap)


_CAN = ("Giáp", "Ất", "Bính", "Đinh", "Mậu", "Kỷ", "Canh", "Tân", "Nhâm", "Quý")
_CHI = ("Tý", "Sửu", "Dần", "Mão", "Thìn", "Tỵ", "Ngọ", "Mùi", "Thân", "Dậu", "Tuất", "Hợi")


def can_chi_year(lunar_year: int) -> str:
    """Tên năm can chi, ví dụ 2026 -> 'Bính Ngọ' (mốc 1984 = Giáp Tý)."""
    return f"{_CAN[(lunar_year - 4) % 10]} {_CHI[(lunar_year - 4) % 12]}"
