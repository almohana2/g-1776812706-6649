"""حدود الوقت — التخزين UTC والعرض والحساب بتوقيت الموقع (SRS §10.7، §17)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

__all__ = [
    "utcnow",
    "to_local",
    "to_utc",
    "local_day_bounds",
    "local_month_bounds",
    "previous_month",
    "month_key",
    "parse_month_key",
    "format_duration_ar",
    "format_duration_compact",
    "format_hm",
    "MONTHS_AR",
]

MONTHS_AR = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)


def _tz(timezone_name: str | None = None) -> ZoneInfo:
    return ZoneInfo(timezone_name) if timezone_name else get_settings().tzinfo


def utcnow() -> datetime:
    """اللحظة الحالية بصيغة UTC واعية بالمنطقة الزمنية."""
    return datetime.now(tz=UTC)


def to_utc(moment: datetime) -> datetime:
    """أي وقت → UTC. الوقت بلا منطقة زمنية يُفترض أنه UTC أصلًا."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def to_local(moment: datetime, timezone_name: str | None = None) -> datetime:
    """UTC → توقيت الموقع، لأغراض العرض وحدود اليوم والشهر."""
    return to_utc(moment).astimezone(_tz(timezone_name))


def local_day_bounds(
    day: date, timezone_name: str | None = None
) -> tuple[datetime, datetime]:
    """نصف مفتوح ``[بداية اليوم، بداية اليوم التالي)`` بصيغة UTC."""
    tzinfo = _tz(timezone_name)
    start = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
    return to_utc(start), to_utc(start + timedelta(days=1))


def local_month_bounds(
    year: int, month: int, timezone_name: str | None = None
) -> tuple[datetime, datetime]:
    """نصف مفتوح ``[أول الشهر، أول الشهر التالي)`` بصيغة UTC."""
    tzinfo = _tz(timezone_name)
    start = datetime(year, month, 1, tzinfo=tzinfo)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tzinfo)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tzinfo)
    return to_utc(start), to_utc(end)


def parse_month_key(value: str) -> tuple[int, int]:
    """``"2026-07"`` → ``(2026, 7)``."""
    parts = value.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"صيغة الشهر يجب أن تكون YYYY-MM وليست {value!r}")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"صيغة الشهر يجب أن تكون YYYY-MM وليست {value!r}") from exc
    if not 1 <= month <= 12 or not 2000 <= year <= 2999:
        raise ValueError(f"شهر خارج النطاق: {value!r}")
    return year, month


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def previous_month(
    moment: datetime | None = None, timezone_name: str | None = None
) -> tuple[int, int]:
    """الشهر السابق للحظة معينة، محسوبًا بتوقيت الموقع."""
    local = to_local(moment or utcnow(), timezone_name)
    first = local.replace(day=1)
    last_month = first - timedelta(days=1)
    return last_month.year, last_month.month


def month_label_ar(year: int, month: int) -> str:
    return f"{MONTHS_AR[month - 1]} {year}"


def format_duration_ar(seconds: float) -> str:
    """``5400`` → ``"1 ساعة 30 دقيقة"`` (SRS §12.3)."""
    total = int(max(0, round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours} ساعة {minutes} دقيقة"
    if hours:
        return f"{hours} ساعة"
    if minutes and secs:
        return f"{minutes} دقيقة {secs} ثانية"
    if minutes:
        return f"{minutes} دقيقة"
    return f"{secs} ثانية"


def format_duration_compact(seconds: float) -> str:
    """صيغة ضيقة للجداول: ``11:06 س`` أو ``45 د`` أو ``30 ث``.

    الجدول الشهري يحمل أحد عشر عمودًا على صفحة A4؛ الصيغة المطوّلة
    ("11 ساعة 6 دقيقة") تكسر الأعمدة على سطرين وتدفع آخرها خارج الصفحة.
    """
    total = int(max(0, round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d} س"
    if minutes:
        return f"{minutes} د"
    return f"{secs} ث"


def format_hm(seconds: float) -> str:
    """``40000`` → ``"11:06"`` — أرقام فقط بلا حروف.

    تُستخدم حيث تجتمع قيمتان في خلية واحدة: خلط أرقام لاتينية بحروف عربية
    داخل خلية واحدة يجعل ترتيب الاتجاه ثنائي الاتجاه يقلب الأجزاء، فتظهر
    "1:00 / 10 د س". الوحدة تُكتب في ترويسة العمود بدل كل خلية.
    """
    total = int(max(0, round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if not hours and not minutes and total:
        return "0:01"
    return f"{hours}:{minutes:02d}"
