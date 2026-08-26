"""رسوم SVG تُبنى على الخادم (SRS §FR-009).

لماذا SVG مولَّد على الخادم بدل مكتبة رسم في المتصفح: التقرير يجب أن يظهر
متطابقًا في الصفحة وفي PDF (AC-008)، و PDF يُصيَّر بلا متصفح ولا JavaScript.
رسم يُبنى في الخادم يظهر كما هو في الحالتين، ويعمل أيضًا مع سياسة CSP التي
تمنع النصوص الخارجية.

قواعد الشكل المتبعة:

* سلسلة واحدة ⇒ لون واحد لكل الأعمدة، لا تدرّج ولا ألوان بعدد الفئات.
* المقارنة الشهرية ليست محورين على رسم واحد، بل رسوم صغيرة مستقلة لكل مؤشر،
  لأن م³ والساعات وك.و.س لا تشترك في مقياس واحد.
* الأعمدة ≤ 24px بفجوة 2px بلون السطح، أطرافها العليا مستديرة 4px.
* التسمية المباشرة للقيمة القصوى فقط؛ الباقي على المحور والجدول.
* المحاور والشبكة رمادية رفيعة 1px غير متقطعة.
* الاتجاه من اليمين إلى اليسار: أول يوم في الشهر على اليمين.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

__all__ = [
    "ACCENT",
    "COMPARE",
    "column_chart",
    "bar_chart",
    "comparison_charts",
    "coverage_meter",
    "ChartPoint",
]

#: لون السلسلة الواحدة — نسخة أعلى تشبّعًا من تيل الواجهة لتجتاز حد الكروما.
ACCENT = "#0d9488"
#: اللون الثاني في المقارنة الشهرية فقط (زوج مُتحقَّق منه ضد عمى الألوان).
COMPARE = "#eb6834"

SURFACE = "#ffffff"
GRID = "#dbe3e6"
INK = "#14212b"
MUTED = "#5b6b74"

MAX_BAR = 24.0
GAP = 2.0
LABEL_FONT = 11
AXIS_FONT = 10


@dataclass(frozen=True)
class ChartPoint:
    """نقطة واحدة: تسمية المحور، القيمة، ونص التلميح."""

    label: str
    value: float
    tooltip: str = ""


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _nice_ceiling(value: float) -> float:
    """أقرب سقف "مريح" لمحور القيم (1، 2، 2.5، 5، 10 × قوة عشرة)."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return float(candidate)
    return float(10 * magnitude)


def _fmt(value: float, digits: int = 1) -> str:
    if value == int(value) and abs(value) < 10000:
        return f"{int(value):,}"
    return f"{value:,.{digits}f}"


def _empty(message: str = "لا توجد بيانات لعرضها") -> str:
    return f'<p class="chart-empty">{_esc(message)}</p>'


# ----------------------------------------------------------------------
def column_chart(
    points: list[ChartPoint],
    *,
    unit: str = "",
    width: int = 720,
    height: int = 220,
    title: str = "",
    digits: int = 1,
) -> str:
    """أعمدة لسلسلة زمنية واحدة، أول قيمة على اليمين."""
    points = [point for point in points if point is not None]
    if not points or all(point.value <= 0 for point in points):
        return _empty()

    pad_top, pad_bottom, pad_side = 18, 26, 46
    plot_w = width - pad_side * 2
    plot_h = height - pad_top - pad_bottom
    top = _nice_ceiling(max(point.value for point in points))
    band = plot_w / len(points)
    bar_w = max(2.0, min(MAX_BAR, band - GAP))
    peak = max(range(len(points)), key=lambda index: points[index].value)

    parts: list[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" preserveAspectRatio="xMidYMid meet" '
        f'aria-label="{_esc(title or "رسم بياني")}">',
        f"<desc>{_esc(title)}</desc>",
    ]

    # الشبكة وقيم المحور على اليمين (اتجاه القراءة).
    for step in range(5):
        value = top * step / 4
        y = pad_top + plot_h - (plot_h * step / 4)
        parts.append(
            f'<line x1="{pad_side}" y1="{y:.1f}" x2="{width - pad_side}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{width - pad_side + 6}" y="{y + 3:.1f}" font-size="{AXIS_FONT}" '
            f'fill="{MUTED}" text-anchor="start">{_fmt(value, digits)}</text>'
        )

    for index, point in enumerate(points):
        # الفهرس صفر على اليمين: نطرح من العرض بدل أن نضيف إليه.
        centre = width - pad_side - (index + 0.5) * band
        bar_h = 0.0 if top <= 0 else plot_h * (point.value / top)
        y = pad_top + plot_h - bar_h
        x = centre - bar_w / 2
        radius = min(4.0, bar_w / 2, max(0.0, bar_h))
        tooltip = point.tooltip or f"{point.label}: {_fmt(point.value, digits)} {unit}".strip()
        if bar_h > 0.5:
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                f'rx="{radius:.1f}" fill="{ACCENT}"><title>{_esc(tooltip)}</title></rect>'
            )
        # تسمية كل خامس يوم فقط حتى لا يتزاحم المحور.
        if index % 5 == 0 or index == len(points) - 1:
            parts.append(
                f'<text x="{centre:.1f}" y="{height - 8}" font-size="{AXIS_FONT}" '
                f'fill="{MUTED}" text-anchor="middle">{_esc(point.label)}</text>'
            )
        if index == peak and point.value > 0:
            parts.append(
                f'<text x="{centre:.1f}" y="{y - 5:.1f}" font-size="{LABEL_FONT}" '
                f'fill="{INK}" text-anchor="middle">{_fmt(point.value, digits)}</text>'
            )

    parts.append(
        f'<line x1="{pad_side}" y1="{pad_top + plot_h}" x2="{width - pad_side}" '
        f'y2="{pad_top + plot_h}" stroke="{GRID}" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


# ----------------------------------------------------------------------
def bar_chart(
    points: list[ChartPoint],
    *,
    unit: str = "",
    title: str = "",
    digits: int = 2,
) -> str:
    """أعمدة أفقية للمقارنة بين المحابس، مبنية بـHTML لا بـSVG.

    السبب: أسماء المحابس عربية، ومحرّك PDF لا يعيد ترتيب النص ثنائي الاتجاه
    داخل ``<text>`` في SVG فتظهر الحروف معكوسة. نص HTML يُشكَّل ويُرتَّب
    صحيحًا في المتصفح وفي PDF معًا، والعمود نفسه مجرد مستطيل ملوّن لا يحتاج
    SVG أصلًا.
    """
    points = [point for point in points if point is not None]
    if not points or all(point.value <= 0 for point in points):
        return _empty()

    ordered = sorted(points, key=lambda point: point.value, reverse=True)
    top = _nice_ceiling(max(point.value for point in ordered))
    rows: list[str] = [f'<div class="hbars" role="img" aria-label="{_esc(title)}">']
    for point in ordered:
        width = 0.0 if top <= 0 else 100.0 * point.value / top
        rows.append(
            '<div class="hbar-row">'
            f'<span class="hbar-label">{_esc(point.label)}</span>'
            '<span class="hbar-track">'
            f'<span class="hbar-fill" style="width:{width:.2f}%;background:{ACCENT}"'
            f' title="{_esc(point.tooltip or point.label)}"></span>'
            "</span>"
            f'<span class="hbar-value">{_fmt(point.value, digits)} {_esc(unit)}</span>'
            "</div>"
        )
    rows.append("</div>")
    return "".join(rows)


# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ComparisonItem:
    """مؤشر واحد بقيمتين: الشهر الحالي والشهر السابق."""

    label: str
    current: float
    previous: float
    unit: str = ""
    digits: int = 2


def comparison_charts(
    items: list[ComparisonItem],
    *,
    current_label: str,
    previous_label: str,
) -> str:
    """رسوم صغيرة مستقلة — لكل مؤشر مقياسه الخاص.

    وضع م³ والساعات وك.و.س على محور واحد يخلق علاقة غير موجودة في
    البيانات؛ الفصل هو الطريقة الصحيحة للمقارنة بين وحدات مختلفة. ومثل
    :func:`bar_chart` تُبنى بـHTML لأن أسماء الأشهر عربية.
    """
    items = [item for item in items if item is not None]
    if not items:
        return _empty("لا يوجد شهر سابق للمقارنة")

    blocks: list[str] = ['<div class="compare-grid">']
    for item in items:
        top = _nice_ceiling(max(item.current, item.previous, 0.0001))
        blocks.append(
            '<div class="compare-item">'
            f'<p class="compare-title">{_esc(item.label)}</p>'
            + _compare_row(item.current, top, ACCENT, current_label,
                           f"{_fmt(item.current, item.digits)} {item.unit}".strip())
            + _compare_row(item.previous, top, COMPARE, previous_label,
                           f"{_fmt(item.previous, item.digits)} {item.unit}".strip())
            + "</div>"
        )
    blocks.append("</div>")
    blocks.append(
        '<div class="chart-legend">'
        f'<span><i style="background:{ACCENT}"></i>{_esc(current_label)}</span>'
        f'<span><i style="background:{COMPARE}"></i>{_esc(previous_label)}</span>'
        "</div>"
    )
    return "".join(blocks)


def _compare_row(value: float, top: float, colour: str, name: str, text: str) -> str:
    width = 0.0 if top <= 0 else 100.0 * max(value, 0.0) / top
    return (
        '<div class="compare-row">'
        f'<span class="compare-name">{_esc(name)}</span>'
        '<span class="hbar-track">'
        f'<span class="hbar-fill" style="width:{width:.2f}%;background:{colour}"'
        f' title="{_esc(name)}: {_esc(text)}"></span>'
        "</span>"
        f'<span class="compare-value">{_esc(text)}</span>'
        "</div>"
    )


# ----------------------------------------------------------------------
def coverage_meter(percent: float, *, width: int = 720, height: int = 34) -> str:
    """مقياس نسبة واحدة مقابل حدّها — أنسب من رسم بياني لرقم واحد."""
    percent = max(0.0, min(100.0, float(percent)))
    if percent >= 99:
        tone = "#15803d"
    elif percent >= 95:
        tone = ACCENT
    elif percent >= 85:
        tone = "#c2410c"
    else:
        tone = "#b91c1c"
    filled = (width - 8) * percent / 100.0
    x = width - 4 - filled
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="تغطية البيانات {percent:.1f}%">'
        f'<rect x="4" y="8" width="{width - 8}" height="14" rx="7" fill="#eef2f3"/>'
        f'<rect x="{x:.1f}" y="8" width="{filled:.1f}" height="14" rx="7" fill="{tone}">'
        f"<title>تغطية البيانات {percent:.1f}%</title></rect>"
        f'<text x="4" y="{height - 4}" font-size="{AXIS_FONT}" fill="{MUTED}" '
        f'text-anchor="start">0%</text>'
        f'<text x="{width - 4}" y="{height - 4}" font-size="{AXIS_FONT}" fill="{INK}" '
        f'text-anchor="end">{percent:.1f}%</text>'
        "</svg>"
    )
