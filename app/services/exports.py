"""تصدير التقرير: HTML و PDF و CSV من نفس المصدر (SRS §FR-010، AC-008).

الثلاثة تُبنى من ``summary_json`` المجمّد نفسه، فتتطابق الإجماليات بينها
بالضرورة لا بالمصادفة.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.templating import read_inline_css, templates
from app.core.time import MONTHS_AR
from app.services.charts import (
    ChartPoint,
    ComparisonItem,
    bar_chart,
    column_chart,
    comparison_charts,
    coverage_meter,
)

logger = get_logger(__name__)

#: يُبقي الاسم صالحًا كاسم ملف على كل نظام.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9؀-ۿ_-]+")


class PdfUnavailable(RuntimeError):
    """تعذّر توليد PDF — تبقى صفحة HTML متاحة (SRS §20)."""


def safe_slug(name: str) -> str:
    slug = _SAFE_NAME.sub("-", (name or "report").strip()).strip("-")
    return slug or "report"


def report_filename(controller_name: str, month: str, kind: str, suffix: str) -> str:
    """مثال: ``ALMOHANA-irrigation-report-2026-07.pdf``."""
    return f"{safe_slug(controller_name)}-irrigation-{kind}-{month}.{suffix}"


def month_label_ar(month: str) -> str:
    try:
        year, number = month.split("-")
        return f"{MONTHS_AR[int(number) - 1]} {int(year)}"
    except (ValueError, IndexError):
        return month


# ----------------------------------------------------------------------
# الرسوم
# ----------------------------------------------------------------------
def _day_number(iso_day: str) -> str:
    try:
        return str(date.fromisoformat(iso_day).day)
    except ValueError:
        return iso_day


def report_charts(payload: dict[str, Any]) -> dict[str, str]:
    """يبني رسوم التقرير من الحمولة المجمّدة."""
    daily = payload.get("daily") or []
    water_points = [
        ChartPoint(
            label=_day_number(item["day"]),
            value=item["water_liters"] / 1000.0,
            tooltip=f"{item['day']}: {item['water_liters'] / 1000.0:,.2f} م³ "
            f"({item['run_count']} تشغيلة)",
        )
        for item in daily
    ]
    hour_points = [
        ChartPoint(
            label=_day_number(item["day"]),
            value=item["runtime_seconds"] / 3600.0,
            tooltip=f"{item['day']}: {item['runtime_seconds'] / 3600.0:,.2f} ساعة",
        )
        for item in daily
    ]
    zone_points = [
        ChartPoint(
            label=zone["name"],
            value=zone["water_liters_estimate"] / 1000.0,
            tooltip=f"{zone['name']}: {zone['water_liters_estimate'] / 1000.0:,.2f} م³ "
            f"({zone['share_percent']:.1f}% من الإجمالي)",
        )
        for zone in payload.get("zones") or []
    ]

    comparison = payload.get("comparison")
    metrics = payload.get("metrics", {})
    comparison_svg = ""
    if comparison:
        comparison_svg = comparison_charts(
            [
                ComparisonItem(
                    "المياه التقديرية",
                    metrics.get("water_estimate_liters", 0) / 1000.0,
                    comparison.get("water_estimate_liters", 0) / 1000.0,
                    "م³",
                ),
                ComparisonItem(
                    "تشغيل المضخة",
                    metrics.get("pump_runtime_seconds", 0) / 3600.0,
                    comparison.get("pump_runtime_seconds", 0) / 3600.0,
                    "ساعة",
                ),
                ComparisonItem(
                    "الطاقة التقديرية",
                    metrics.get("energy_estimate_kwh", 0),
                    comparison.get("energy_estimate_kwh", 0),
                    "ك.و.س",
                    digits=1,
                ),
            ],
            current_label=month_label_ar(payload.get("month", "")),
            previous_label=month_label_ar(comparison.get("month", "")),
        )

    return {
        "daily_water_chart": column_chart(
            water_points, unit="م³", title="استهلاك المياه يوميًا", digits=2
        ),
        "daily_hours_chart": column_chart(
            hour_points, unit="ساعة", title="ساعات التشغيل يوميًا", digits=1
        ),
        "zone_chart": bar_chart(zone_points, unit="م³", title="توزيع المياه بين المحابس"),
        "comparison_chart": comparison_svg,
        "coverage_meter": coverage_meter(
            float(payload.get("coverage", {}).get("percent", 0))
        ),
    }


# ----------------------------------------------------------------------
# HTML و PDF
# ----------------------------------------------------------------------
@dataclass
class RenderedReport:
    html: str
    filename: str


def render_report_html(
    payload: dict[str, Any],
    *,
    request: Any = None,
    user: Any = None,
    show_actions: bool = False,
    standalone: bool = False,
    share_url: str | None = None,
    share_expires: datetime | None = None,
    deliveries: list[Any] | None = None,
) -> str:
    """يصيّر صفحة التقرير.

    ``standalone`` يُضمِّن CSS داخل الصفحة — تحتاجه نسخة PDF لأنها تُصيَّر
    بلا خادم يقدّم ``/static``.
    """
    context: dict[str, Any] = {
        "report": payload,
        "user": user,
        "show_actions": show_actions,
        "share_url": share_url,
        "share_expires": share_expires,
        "deliveries": deliveries or [],
        "site_name": payload.get("controller", {}).get("name"),
        "active": "reports",
        "inline_css": read_inline_css() if standalone else None,
    }
    context.update(report_charts(payload))
    template = templates.env.get_template("report_monthly.html")
    context["request"] = request
    return template.render(**context)


def render_report_pdf(payload: dict[str, Any], *, base_url: str | None = None) -> bytes:
    """يحوّل الصفحة إلى PDF مع الحفاظ على العربية واتجاه RTL (SRS §NFR-007)."""
    try:
        from weasyprint import HTML  # استيراد كسول: مكتبة ثقيلة
    except Exception as exc:  # pragma: no cover - بيئة بلا WeasyPrint
        raise PdfUnavailable(f"WeasyPrint غير متاح: {exc}") from exc

    html = render_report_html(payload, standalone=True, show_actions=False)
    try:
        return HTML(string=html, base_url=base_url or ".").write_pdf()
    except Exception as exc:
        logger.exception("report.pdf_failed")
        raise PdfUnavailable(str(exc)) from exc


# ----------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------
CSV_HEADERS = [
    "month",
    "controller",
    "zone_number",
    "zone_name",
    "run_count",
    "runtime_seconds",
    "runtime_hours",
    "average_seconds_per_active_day",
    "active_days",
    "longest_run_seconds",
    "shortest_run_seconds",
    "flow_lpm",
    "water_liters_estimate",
    "water_liters_min",
    "water_liters_max",
    "water_m3_estimate",
    "share_percent",
    "low_confidence_runs",
]


def render_zones_csv(payload: dict[str, Any]) -> bytes:
    """CSV بترميز UTF-8 مع BOM ليفتحه Excel بالعربية سليمة (SRS §FR-010)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    controller = payload.get("controller", {}).get("name", "")
    month = payload.get("month", "")

    for zone in payload.get("zones") or []:
        writer.writerow(
            [
                month,
                controller,
                zone.get("number") if zone.get("number") is not None else "",
                zone.get("name", ""),
                zone.get("run_count", 0),
                zone.get("runtime_seconds", 0),
                f"{zone.get('runtime_hours', 0):.3f}",
                zone.get("average_seconds_per_active_day", 0),
                zone.get("active_days", 0),
                zone.get("longest_run_seconds", 0),
                zone.get("shortest_run_seconds", 0),
                f"{zone.get('flow_lpm', 0):.2f}",
                f"{zone.get('water_liters_estimate', 0):.2f}",
                f"{zone.get('water_liters_min', 0):.2f}",
                f"{zone.get('water_liters_max', 0):.2f}",
                f"{zone.get('water_liters_estimate', 0) / 1000:.3f}",
                f"{zone.get('share_percent', 0):.2f}",
                zone.get("low_confidence_runs", 0),
            ]
        )

    metrics = payload.get("metrics", {})
    writer.writerow([])
    writer.writerow(["# الإجماليات"])
    writer.writerow(["pump_runtime_seconds", metrics.get("pump_runtime_seconds", 0)])
    writer.writerow(["zone_runtime_seconds", metrics.get("zone_runtime_seconds", 0)])
    writer.writerow(["water_estimate_liters", f"{metrics.get('water_estimate_liters', 0):.2f}"])
    writer.writerow(["water_min_liters", f"{metrics.get('water_min_liters', 0):.2f}"])
    writer.writerow(["water_max_liters", f"{metrics.get('water_max_liters', 0):.2f}"])
    writer.writerow(["energy_estimate_kwh", f"{metrics.get('energy_estimate_kwh', 0):.3f}"])
    writer.writerow(["event_count", metrics.get("event_count", 0)])
    writer.writerow(["coverage_percent", f"{metrics.get('coverage_percent', 0):.2f}"])
    writer.writerow([])
    writer.writerow(["# " + payload.get("disclaimer", "")])

    # BOM صريح بالبايتات — Excel يحتاجه ليقرأ UTF-8 عربيًا صحيحًا.
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
