"""الرسوم وصيغ العرض العربية (SRS §12.3، §FR-009)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.templating import fmt_m3, fmt_number, fmt_signed_percent, fmt_volume
from app.core.time import (
    format_duration_ar,
    format_duration_compact,
    format_hm,
    local_month_bounds,
    month_key,
    parse_month_key,
    previous_month,
)
from app.services.charts import (
    ACCENT,
    ChartPoint,
    ComparisonItem,
    bar_chart,
    column_chart,
    comparison_charts,
    coverage_meter,
)


class TestDurations:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (5400, "1 ساعة 30 دقيقة"),
            (3600, "1 ساعة"),
            (90, "1 دقيقة 30 ثانية"),
            (120, "2 دقيقة"),
            (45, "45 ثانية"),
            (0, "0 ثانية"),
            (-5, "0 ثانية"),
        ],
    )
    def test_long_form(self, seconds, expected):
        assert format_duration_ar(seconds) == expected

    def test_compact_form_fits_a_table_cell(self):
        assert format_duration_compact(40000) == "11:06 س"
        assert format_duration_compact(2700) == "45 د"
        assert format_duration_compact(30) == "30 ث"

    def test_numeric_form_has_no_letters(self):
        # الخلية التي تجمع قيمتين لا تحتمل حروفًا: الاتجاه يقلبها.
        assert format_hm(40000) == "11:06"
        assert format_hm(2700) == "0:45"
        assert format_hm(30) == "0:01"
        assert format_hm(0) == "0:00"
        assert all(char not in format_hm(40000) for char in "سدث")


class TestNumbers:
    def test_volume_switches_unit_below_one_cubic_metre(self):
        assert fmt_volume(750) == "750 لتر"
        assert fmt_volume(8400) == "8.40 م³"
        assert fmt_volume(None) == "—"

    def test_cubic_metres_conversion(self):
        assert fmt_m3(8400) == "8.40"
        assert fmt_m3(None) == "—"

    def test_missing_numbers_show_a_dash_not_zero(self):
        assert fmt_number(None) == "—"
        assert fmt_number(1234.5) == "1,234.50"

    def test_change_carries_an_explicit_sign(self):
        # اللون وحده لا ينقل الاتجاه (SRS §12.3).
        assert fmt_signed_percent(12.0) == "+12.0%"
        assert fmt_signed_percent(-8.5) == "-8.5%"
        assert fmt_signed_percent(None) == "—"


class TestMonths:
    def test_parse_and_format(self):
        assert parse_month_key("2026-07") == (2026, 7)
        assert month_key(2026, 7) == "2026-07"

    @pytest.mark.parametrize("bad", ["2026", "2026-13", "july", "2026-00", "1999-05"])
    def test_bad_months_are_rejected(self, bad):
        with pytest.raises(ValueError):
            parse_month_key(bad)

    def test_month_bounds_are_local_midnight_expressed_in_utc(self):
        start, end = local_month_bounds(2026, 7, "Asia/Muscat")
        assert start == datetime(2026, 6, 30, 20, tzinfo=UTC)
        assert end == datetime(2026, 7, 31, 20, tzinfo=UTC)

    def test_december_rolls_over(self):
        _, end = local_month_bounds(2026, 12, "UTC")
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    def test_previous_month_uses_local_time(self):
        # 00:30 في مسقط أول أغسطس ما زال 31 يوليو بـUTC؛ الشهر السابق يوليو.
        moment = datetime(2026, 7, 31, 20, 30, tzinfo=UTC)
        assert previous_month(moment, "Asia/Muscat") == (2026, 7)


class TestCharts:
    def test_column_chart_renders_one_bar_per_point(self):
        points = [ChartPoint(str(day), day * 1.5) for day in range(1, 32)]
        svg = column_chart(points, unit="م³", title="اختبار")
        assert svg.startswith("<svg")
        assert svg.count("<rect") == 31
        assert 'role="img"' in svg

    def test_charts_say_so_when_there_is_nothing_to_show(self):
        assert "لا توجد بيانات" in column_chart([], unit="م³")
        assert "لا توجد بيانات" in bar_chart([ChartPoint("أ", 0)], unit="م³")
        assert "لا يوجد شهر سابق" in comparison_charts(
            [], current_label="أ", previous_label="ب"
        )

    def test_a_single_series_uses_one_colour_for_every_bar(self):
        # لون لكل عمود يزدوج مع طول العمود ولا يضيف معلومة.
        svg = column_chart([ChartPoint(str(i), i) for i in range(1, 6)])
        assert svg.count(ACCENT) == 5

    def test_bar_chart_labels_are_html_not_svg_text(self):
        # نص SVG لا يُعاد ترتيبه ثنائي الاتجاه في PDF فتنقلب الحروف.
        html = bar_chart([ChartPoint("المسطح الأمامي", 12.5)], unit="م³")
        assert "<svg" not in html
        assert "المسطح الأمامي" in html

    def test_bars_are_sorted_descending(self):
        html = bar_chart(
            [ChartPoint("صغير", 1.0), ChartPoint("كبير", 9.0)], unit="م³"
        )
        assert html.index("كبير") < html.index("صغير")

    def test_comparison_keeps_each_metric_on_its_own_scale(self):
        import re

        html = comparison_charts(
            [
                ComparisonItem("المياه", 250, 200, "م³"),
                ComparisonItem("الساعات", 60, 48, "ساعة"),
            ],
            current_label="يوليو",
            previous_label="يونيو",
        )
        widths = [float(value) for value in re.findall(r"width:([\d.]+)%", html)]
        assert len(widths) == 4
        # نسبة العمودين داخل كل مؤشر تعكس قيمتيه هو، لا قياسًا مشتركًا:
        # 200/250 و48/60 كلاهما 0.8 هنا بالمصادفة، فنتحقق من كل زوج وحده.
        assert widths[1] / widths[0] == pytest.approx(200 / 250, rel=1e-3)
        assert widths[3] / widths[2] == pytest.approx(48 / 60, rel=1e-3)
        # الدليل على استقلال المقياس: 60 ساعة تملأ معظم مسارها رغم أنها
        # ربع قيمة المياه؛ على محور مشترك كانت ستظهر بنحو 24% فقط.
        assert widths[2] > 50

    def test_hostile_labels_are_escaped(self):
        html = bar_chart([ChartPoint("<script>x</script>", 5.0)], unit="م³")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_coverage_meter_clamps_and_labels(self):
        assert "100.0%" in coverage_meter(140)
        assert "0.0%" in coverage_meter(-5)
        assert 'role="img"' in coverage_meter(97.2)
