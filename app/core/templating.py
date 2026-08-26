"""تهيئة Jinja2 والمرشّحات العربية المشتركة بين الصفحات و PDF."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.core.time import (
    MONTHS_AR,
    format_duration_ar,
    format_duration_compact,
    format_hm,
    to_local,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

ARABIC_DIGITS = str.maketrans("0123456789", "0123456789")


def fmt_number(value: Any, digits: int = 2, dash: str = "—") -> str:
    """رقم بفواصل آلاف ومنازل عشرية ثابتة، و``—`` عند غياب القيمة."""
    if value is None:
        return dash
    if isinstance(value, Decimal):
        value = float(value)
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return dash


def fmt_volume(liters: Any) -> str:
    """لترات تحت المتر المكعب، وأمتار مكعبة فوقه (SRS §12.3)."""
    if liters is None:
        return "—"
    if isinstance(liters, Decimal):
        liters = float(liters)
    liters = float(liters)
    if liters < 1000:
        return f"{liters:,.0f} لتر"
    return f"{liters / 1000:,.2f} م³"


def fmt_m3(liters: Any, digits: int = 2) -> str:
    if liters is None:
        return "—"
    return f"{float(liters) / 1000:,.{digits}f}"


def fmt_datetime(moment: datetime | str | None, with_seconds: bool = False) -> str:
    if moment is None:
        return "—"
    if isinstance(moment, str):
        try:
            parsed = datetime.fromisoformat(moment)
        except ValueError:
            return moment  # نص غير قابل للتحليل يُعرض كما هو
        moment = parsed
    local = to_local(moment)
    pattern = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return local.strftime(pattern)


def fmt_time(moment: datetime | str | None) -> str:
    if moment is None:
        return "—"
    if isinstance(moment, str):
        try:
            parsed = datetime.fromisoformat(moment)
        except ValueError:
            return moment  # نص غير قابل للتحليل يُعرض كما هو
        moment = parsed
    return to_local(moment).strftime("%H:%M")


def fmt_month(value: str) -> str:
    """``2026-07`` → ``يوليو 2026``."""
    try:
        year, month = value.split("-")
        return f"{MONTHS_AR[int(month) - 1]} {int(year)}"
    except (ValueError, IndexError):
        return value


def fmt_percent(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}%"


def fmt_signed_percent(value: Any, digits: int = 1) -> str:
    """نسبة التغير مع إشارة صريحة، لأن اللون وحده لا يكفي (SRS §12.3)."""
    if value is None:
        return "—"
    number = float(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,.{digits}f}%"


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        {
            "number": fmt_number,
            "volume": fmt_volume,
            "m3": fmt_m3,
            "duration": format_duration_ar,
            "duration_compact": format_duration_compact,
            "hm": format_hm,
            "datetime": fmt_datetime,
            "time": fmt_time,
            "month_ar": fmt_month,
            "percent": fmt_percent,
            "signed_percent": fmt_signed_percent,
        }
    )
    templates.env.globals.update(
        {
            "app_name": get_settings().app_name,
            "timezone": get_settings().report_timezone,
        }
    )
    return templates


templates = build_templates()


def page(
    request: Request,
    name: str,
    context: dict[str, Any],
    status_code: int = 200,
) -> Response:
    """اختصار لعرض صفحة مع السياق المشترك."""
    payload = {"request": request, "timezone": get_settings().report_timezone}
    payload.update(context)
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


def read_inline_css() -> str:
    """نص CSS كاملًا — يُضمَّن داخل صفحة الـPDF لأنها تُصيَّر بلا خادم."""
    return (STATIC_DIR / "app.css").read_text(encoding="utf-8")
