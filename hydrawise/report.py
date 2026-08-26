"""Rendering: the usage report as text, CSV, JSON, or an emailable HTML page.

Per-person output is bilingual (``en``/``ar``) because the people receiving a
monthly bill are not necessarily the person running the tool. Arabic pages are
rendered right-to-left.
"""

from __future__ import annotations

import csv
import html
import io
from typing import Any, Dict, List, Optional

from .usage import PersonUsage, UsageReport, ZoneUsage

__all__ = [
    "render_summary",
    "render_person_text",
    "render_person_html",
    "render_csv",
    "to_dict",
    "format_duration",
]

_LABELS = {
    "en": {
        "title": "Irrigation usage report",
        "period": "Period",
        "zone": "Zone",
        "runs": "Runs",
        "time": "Run time",
        "water": "Water (m³)",
        "energy": "Energy (kWh)",
        "cost": "Cost",
        "total": "Total",
        "hello": "Hello {name},",
        "intro": "Here is your irrigation usage for {period}.",
        "share": "Share of the site total",
        "no_usage": "No watering was recorded for your zones this period.",
        "basis": (
            "Water volume is estimated from each valve's configured flow rate "
            "multiplied by its run time; energy from the pump rating multiplied "
            "by the same run time."
        ),
        "unassigned": "Unassigned zones",
        "warnings": "Notes",
        "generated": "Generated",
    },
    "ar": {
        "title": "تقرير استهلاك الري",
        "period": "الفترة",
        "zone": "المحبس",
        "runs": "عدد التشغيلات",
        "time": "ساعات التشغيل",
        "water": "المياه (م³)",
        "energy": "الكهرباء (ك.و.س)",
        "cost": "التكلفة",
        "total": "الإجمالي",
        "hello": "مرحباً {name}،",
        "intro": "هذا استهلاكك من الري خلال {period}.",
        "share": "نسبتك من إجمالي الموقع",
        "no_usage": "لم يُسجَّل أي ري لمحابسك خلال هذه الفترة.",
        "basis": (
            "حجم المياه محسوب من معدل تدفق كل محبس مضروباً في ساعات تشغيله، "
            "والكهرباء من قدرة المضخة مضروبة في نفس الساعات."
        ),
        "unassigned": "محابس غير مُسندة",
        "warnings": "ملاحظات",
        "generated": "تاريخ الإصدار",
    },
}


def format_duration(seconds: int) -> str:
    """``9015`` → ``"2h 30m"``; short runs keep their seconds."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _money(value: float, currency: str) -> str:
    if not currency:
        return f"{value:,.2f}"
    return f"{value:,.2f} {currency}"


def _number(value: Optional[float], digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    return f"{value:,.{digits}f}"


def _zone_rows(zones: List[ZoneUsage]) -> List[List[str]]:
    return [
        [
            zone.name,
            str(zone.runs),
            format_duration(zone.seconds),
            _number(zone.cubic_meters, 3),
            _number(zone.kwh, 2),
        ]
        for zone in zones
    ]


def _table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return ""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    rule = "  ".join("-" * width for width in widths)
    body = "\n".join(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in rows
    )
    return f"{line}\n{rule}\n{body}"


def render_summary(report: UsageReport, *, language: str = "en") -> str:
    """The whole-site view, for the operator's terminal."""
    labels = _LABELS.get(language, _LABELS["en"])
    lines = [
        f"{labels['title']} — {report.period}",
        f"{report.start.isoformat()} → {report.end.isoformat()}",
        "",
    ]
    for person in report.people:
        header = f"{person.person.display_name}"
        if person.person.email:
            header += f" <{person.person.email}>"
        lines.append(header)
        table = _table(
            [labels["zone"], labels["runs"], labels["time"], labels["water"], labels["energy"]],
            _zone_rows(person.zones),
        )
        if table:
            lines.extend("  " + row for row in table.splitlines())
        lines.append(
            "  {total}: {time} · {water} m³ · {energy} kWh · {cost}".format(
                total=labels["total"],
                time=format_duration(person.seconds),
                water=_number(person.cubic_meters, 3),
                energy=_number(person.kwh, 2),
                cost=_money(person.total_cost, report.currency),
            )
        )
        lines.append("")

    if report.unassigned:
        lines.append(labels["unassigned"])
        table = _table(
            [labels["zone"], labels["runs"], labels["time"], labels["water"], labels["energy"]],
            _zone_rows(report.unassigned),
        )
        if table:
            lines.extend("  " + row for row in table.splitlines())
        lines.append("")

    lines.append(
        "{total}: {time} · {water} m³ · {energy} kWh · {cost}".format(
            total=labels["total"].upper(),
            time=format_duration(report.seconds),
            water=_number(report.cubic_meters, 3),
            energy=_number(report.kwh, 2),
            cost=_money(report.total_cost, report.currency),
        )
    )
    if report.warnings:
        lines.append("")
        lines.append(labels["warnings"] + ":")
        lines.extend(f"  - {warning}" for warning in report.warnings)
    return "\n".join(lines)


def render_person_text(
    person: PersonUsage, report: UsageReport, *, language: Optional[str] = None
) -> str:
    """The plain-text body of one person's monthly mail."""
    language = language or person.person.language
    labels = _LABELS.get(language, _LABELS["en"])
    lines = [
        labels["hello"].format(name=person.person.display_name),
        "",
        labels["intro"].format(period=report.period),
        "",
    ]
    if person.seconds == 0:
        lines.append(labels["no_usage"])
    else:
        table = _table(
            [labels["zone"], labels["runs"], labels["time"], labels["water"], labels["energy"]],
            _zone_rows(person.zones),
        )
        lines.append(table)
        lines.append("")
        lines.append(
            f"{labels['time']}: {format_duration(person.seconds)}"
        )
        lines.append(f"{labels['water']}: {_number(person.cubic_meters, 3)}")
        lines.append(f"{labels['energy']}: {_number(person.kwh, 2)}")
        lines.append(f"{labels['cost']}: {_money(person.total_cost, report.currency)}")
        share = person.share_of(report.cubic_meters, person.cubic_meters)
        if report.cubic_meters:
            lines.append(f"{labels['share']}: {share:.1f}%")
    lines.extend(["", labels["basis"]])
    if report.generated_at:
        lines.extend(["", f"{labels['generated']}: {report.generated_at.isoformat()}"])
    return "\n".join(lines)


_HTML_TEMPLATE = """<!doctype html>
<html lang="{lang}" dir="{dir}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:24px;background:#f4f6f5;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#1c2b27;">
<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px;">
<h1 style="margin:0 0 4px;font-size:20px;">{title}</h1>
<p style="margin:0 0 20px;color:#5b6b66;font-size:14px;">{period_label}: {period}</p>
<p style="margin:0 0 16px;">{greeting}</p>
<p style="margin:0 0 20px;">{intro}</p>
{table}
{totals}
<p style="margin:24px 0 0;color:#5b6b66;font-size:12px;line-height:1.6;">{basis}</p>
{generated}
</div>
</body>
</html>
"""


def render_person_html(
    person: PersonUsage, report: UsageReport, *, language: Optional[str] = None
) -> str:
    """The HTML body of one person's monthly mail."""
    language = language or person.person.language
    labels = _LABELS.get(language, _LABELS["en"])
    direction = "rtl" if language == "ar" else "ltr"
    align = "right" if direction == "rtl" else "left"

    if person.seconds == 0:
        table = (
            f'<p style="margin:0;padding:16px;background:#f4f6f5;border-radius:8px;">'
            f'{html.escape(labels["no_usage"])}</p>'
        )
        totals = ""
    else:
        header_cells = "".join(
            f'<th style="text-align:{align};padding:8px 10px;border-bottom:2px solid #dfe5e3;'
            f'font-size:13px;color:#5b6b66;">{html.escape(text)}</th>'
            for text in (
                labels["zone"],
                labels["runs"],
                labels["time"],
                labels["water"],
                labels["energy"],
            )
        )
        body_rows = "".join(
            "<tr>"
            + "".join(
                f'<td style="text-align:{align};padding:8px 10px;border-bottom:1px solid #eef1f0;'
                f'font-size:14px;">{html.escape(cell)}</td>'
                for cell in row
            )
            + "</tr>"
            for row in _zone_rows(person.zones)
        )
        table = (
            '<table role="presentation" style="width:100%;border-collapse:collapse;">'
            f"<thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody></table>"
        )
        pairs = [
            (labels["time"], format_duration(person.seconds)),
            (labels["water"], _number(person.cubic_meters, 3)),
            (labels["energy"], _number(person.kwh, 2)),
            (labels["cost"], _money(person.total_cost, report.currency)),
        ]
        if report.cubic_meters:
            pairs.append(
                (
                    labels["share"],
                    f"{person.share_of(report.cubic_meters, person.cubic_meters):.1f}%",
                )
            )
        totals = (
            '<div style="margin-top:20px;padding:16px;background:#f4f6f5;border-radius:8px;">'
            + "".join(
                f'<div style="display:flex;justify-content:space-between;font-size:14px;'
                f'padding:4px 0;"><span style="color:#5b6b66;">{html.escape(name)}</span>'
                f"<strong>{html.escape(value)}</strong></div>"
                for name, value in pairs
            )
            + "</div>"
        )

    generated = ""
    if report.generated_at:
        generated = (
            f'<p style="margin:8px 0 0;color:#93a29d;font-size:11px;">'
            f'{html.escape(labels["generated"])}: '
            f'{html.escape(report.generated_at.isoformat(timespec="seconds"))}</p>'
        )

    return _HTML_TEMPLATE.format(
        lang=language,
        dir=direction,
        title=html.escape(labels["title"]),
        period_label=html.escape(labels["period"]),
        period=html.escape(report.period),
        greeting=html.escape(labels["hello"].format(name=person.person.display_name)),
        intro=html.escape(labels["intro"].format(period=report.period)),
        table=table,
        totals=totals,
        basis=html.escape(labels["basis"]),
        generated=generated,
    )


def render_csv(report: UsageReport) -> str:
    """One row per zone — the format a spreadsheet or an accountant wants."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "period",
            "person_id",
            "person_name",
            "person_email",
            "zone",
            "zone_number",
            "relay_id",
            "runs",
            "seconds",
            "hours",
            "cubic_meters",
            "kwh",
            "water_cost",
            "energy_cost",
            "total_cost",
            "currency",
        ]
    )
    for person in report.people:
        for zone in person.zones:
            writer.writerow(
                [
                    report.period,
                    person.person.id,
                    person.person.display_name,
                    person.person.email or "",
                    zone.name,
                    zone.zone_number if zone.zone_number is not None else "",
                    zone.relay_id if zone.relay_id is not None else "",
                    zone.runs,
                    zone.seconds,
                    f"{zone.hours:.4f}",
                    "" if zone.cubic_meters is None else f"{zone.cubic_meters:.4f}",
                    "" if zone.kwh is None else f"{zone.kwh:.4f}",
                    f"{zone.water_cost:.4f}",
                    f"{zone.energy_cost:.4f}",
                    f"{zone.total_cost:.4f}",
                    report.currency,
                ]
            )
    for zone in report.unassigned:
        writer.writerow(
            [
                report.period,
                "",
                "",
                "",
                zone.name,
                zone.zone_number if zone.zone_number is not None else "",
                zone.relay_id if zone.relay_id is not None else "",
                zone.runs,
                zone.seconds,
                f"{zone.hours:.4f}",
                "" if zone.cubic_meters is None else f"{zone.cubic_meters:.4f}",
                "" if zone.kwh is None else f"{zone.kwh:.4f}",
                f"{zone.water_cost:.4f}",
                f"{zone.energy_cost:.4f}",
                f"{zone.total_cost:.4f}",
                report.currency,
            ]
        )
    return buffer.getvalue()


def _zone_dict(zone: ZoneUsage) -> Dict[str, Any]:
    return {
        "key": zone.key,
        "name": zone.name,
        "zone_number": zone.zone_number,
        "relay_id": zone.relay_id,
        "owner": zone.owner_id,
        "runs": zone.runs,
        "seconds": zone.seconds,
        "hours": round(zone.hours, 4),
        "cubic_meters": None if zone.cubic_meters is None else round(zone.cubic_meters, 4),
        "kwh": None if zone.kwh is None else round(zone.kwh, 4),
        "water_cost": round(zone.water_cost, 4),
        "energy_cost": round(zone.energy_cost, 4),
        "total_cost": round(zone.total_cost, 4),
    }


def to_dict(report: UsageReport) -> Dict[str, Any]:
    """The report as plain JSON-serialisable data."""
    return {
        "period": report.period,
        "start": report.start.isoformat(),
        "end": report.end.isoformat(),
        "currency": report.currency,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
        "totals": {
            "seconds": report.seconds,
            "hours": round(report.hours, 4),
            "cubic_meters": round(report.cubic_meters, 4),
            "kwh": round(report.kwh, 4),
            "water_cost": round(report.water_cost, 4),
            "energy_cost": round(report.energy_cost, 4),
            "total_cost": round(report.total_cost, 4),
        },
        "people": [
            {
                "id": person.person.id,
                "name": person.person.display_name,
                "email": person.person.email,
                "language": person.person.language,
                "seconds": person.seconds,
                "hours": round(person.hours, 4),
                "cubic_meters": round(person.cubic_meters, 4),
                "kwh": round(person.kwh, 4),
                "water_cost": round(person.water_cost, 4),
                "energy_cost": round(person.energy_cost, 4),
                "total_cost": round(person.total_cost, 4),
                "zones": [_zone_dict(zone) for zone in person.zones],
            }
            for person in report.people
        ],
        "unassigned": [_zone_dict(zone) for zone in report.unassigned],
        "warnings": report.warnings,
    }
