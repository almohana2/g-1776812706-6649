"""تطابق الصيغ الثلاث وسلامة العربية في التصدير (SRS §FR-010، AC-008، AC-009)."""

from __future__ import annotations

import csv
import io

import pytest

from app.services.exports import (
    render_report_html,
    render_report_pdf,
    render_zones_csv,
    report_filename,
)

PAYLOAD = {
    "controller": {"name": "ALMOHANA", "serial_masked": "********0ABC"},
    "month": "2026-07",
    "timezone": "Asia/Muscat",
    "generated_at": "2026-08-01T00:15:00+00:00",
    "period": {"start": "2026-06-30T20:00:00+00:00", "end": "2026-07-31T20:00:00+00:00"},
    "metrics": {
        "pump_runtime_seconds": 54000,
        "zone_runtime_seconds": 72000,
        "water_estimate_liters": 168000.0,
        "water_min_liters": 96000.0,
        "water_max_liters": 240000.0,
        "energy_estimate_kwh": 60.0,
        "event_count": 40,
        "coverage_percent": 98.5,
        "low_confidence_runs": 0,
        "average_runtime_seconds_per_active_day": 3600,
    },
    "coverage": {"percent": 98.5, "grade": "جيدة", "tone": "info", "gap_seconds": 0, "reasons": []},
    "zones": [
        {
            "zone_id": "z1", "number": 1, "name": "المسطح الأمامي", "flow_lpm": 140,
            "run_count": 20, "runtime_seconds": 40000, "runtime_hours": 11.111,
            "average_seconds_per_active_day": 2000, "active_days": 20,
            "longest_run_seconds": 3600, "shortest_run_seconds": 600,
            "water_liters_estimate": 93333.33, "water_liters_min": 53333.33,
            "water_liters_max": 133333.33, "share_percent": 55.56, "low_confidence_runs": 0,
        },
        {
            "zone_id": "z2", "number": 2, "name": "النخيل", "flow_lpm": 140,
            "run_count": 20, "runtime_seconds": 32000, "runtime_hours": 8.889,
            "average_seconds_per_active_day": 1600, "active_days": 20,
            "longest_run_seconds": 3000, "shortest_run_seconds": 500,
            "water_liters_estimate": 74666.67, "water_liters_min": 42666.67,
            "water_liters_max": 106666.67, "share_percent": 44.44, "low_confidence_runs": 0,
        },
    ],
    "daily": [
        {
            "day": f"2026-07-{day:02d}", "runtime_seconds": 2400,
            "pump_seconds": 2400, "water_liters": 5600.0, "run_count": 2,
        }
        for day in range(1, 32)
    ],
    "highlights": {
        "top_zone_by_water": "المسطح الأمامي", "top_zone_liters": 93333.33,
        "top_day": "2026-07-01", "top_day_liters": 5600.0, "active_days": 31,
    },
    "comparison": {
        "month": "2026-06", "water_estimate_liters": 150000.0,
        "pump_runtime_seconds": 48000, "energy_estimate_kwh": 53.3,
        "event_count": 36, "water_change_percent": 12.0, "pump_change_percent": 12.5,
    },
    "alerts": [{"code": "long_run", "severity": "warn", "text": "تشغيل طويل غير معتاد."}],
    "methodology": {
        "default_flow_lpm": 140, "flow_min_lpm": 80, "flow_max_lpm": 200,
        "pump_input_kw": 4.0, "water_is_estimated": True, "energy_is_estimated": True,
        "uncalibrated_zones": ["النخيل"],
    },
    "disclaimer": "استهلاك المياه تقديري ومحسوب من مدة التشغيل.",
    "has_data": True,
}


class TestHtml:
    def test_page_is_arabic_and_rtl(self):
        html = render_report_html(PAYLOAD)
        assert 'dir="rtl"' in html
        assert 'lang="ar"' in html
        assert "تقرير الري الشهري" in html

    def test_zone_names_appear_unescaped_to_the_reader(self):
        html = render_report_html(PAYLOAD)
        assert "المسطح الأمامي" in html
        assert "النخيل" in html

    def test_the_estimate_disclaimer_is_always_present(self):
        assert PAYLOAD["disclaimer"] in render_report_html(PAYLOAD)

    def test_uncalibrated_zones_are_flagged(self):
        assert "غير معاير" in render_report_html(PAYLOAD)

    def test_standalone_inlines_the_stylesheet(self):
        html = render_report_html(PAYLOAD, standalone=True)
        assert "<style>" in html
        assert "/static/app.css" not in html

    def test_a_month_without_data_says_so_instead_of_showing_zero(self):
        empty = {**PAYLOAD, "has_data": False}
        html = render_report_html(empty)
        assert "لا توجد بيانات كافية" in html

    def test_low_coverage_carries_a_warning(self):
        low = {
            **PAYLOAD,
            "coverage": {**PAYLOAD["coverage"], "percent": 60.0, "tone": "bad"},
            "metrics": {**PAYLOAD["metrics"], "coverage_percent": 60.0},
        }
        html = render_report_html(low)
        assert "أقل من 85%" in html

    def test_script_injection_in_a_zone_name_is_escaped(self):
        hostile = {
            **PAYLOAD,
            "zones": [{**PAYLOAD["zones"][0], "name": "<script>alert(1)</script>"}],
        }
        html = render_report_html(hostile)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestCsv:
    def test_starts_with_a_utf8_bom_for_excel(self):
        assert render_zones_csv(PAYLOAD).startswith(b"\xef\xbb\xbf")

    def test_arabic_names_round_trip(self):
        text = render_zones_csv(PAYLOAD).decode("utf-8-sig")
        assert "المسطح الأمامي" in text

    def test_one_row_per_zone(self):
        text = render_zones_csv(PAYLOAD).decode("utf-8-sig")
        rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("zone_name")]
        assert len(rows) == 2

    def test_ac_008_totals_match_the_json_report(self):
        text = render_zones_csv(PAYLOAD).decode("utf-8-sig")
        rows = [
            row
            for row in csv.DictReader(io.StringIO(text))
            if row.get("zone_name") and row.get("water_liters_estimate")
        ]
        total = sum(float(row["water_liters_estimate"]) for row in rows)
        assert total == pytest.approx(PAYLOAD["metrics"]["water_estimate_liters"], abs=0.05)

    def test_summary_block_carries_the_headline_numbers(self):
        text = render_zones_csv(PAYLOAD).decode("utf-8-sig")
        assert "pump_runtime_seconds" in text
        assert "coverage_percent" in text
        assert PAYLOAD["disclaimer"] in text


class TestPdf:
    def test_pdf_is_produced_and_looks_like_a_pdf(self):
        pdf = render_report_pdf(PAYLOAD)
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 5000

    def test_ac_009_arabic_font_is_embedded(self):
        pypdf = pytest.importorskip("pypdf")
        pdf = render_report_pdf(PAYLOAD)
        reader = pypdf.PdfReader(io.BytesIO(pdf))
        fonts = set()
        for page in reader.pages:
            for _, font in (page.get("/Resources", {}) or {}).get("/Font", {}).items():
                fonts.add(str(font.get_object().get("/BaseFont", "")))
        # بدون خط عربي مضمَّن تظهر الصفحة فارغة أو مربعات في PDF.
        assert any("Arabic" in name or "Noto" in name for name in fonts), fonts


class TestFilenames:
    def test_pattern_matches_the_spec(self):
        assert (
            report_filename("ALMOHANA", "2026-07", "report", "pdf")
            == "ALMOHANA-irrigation-report-2026-07.pdf"
        )
        assert (
            report_filename("ALMOHANA", "2026-07", "zones", "csv")
            == "ALMOHANA-irrigation-zones-2026-07.csv"
        )

    def test_awkward_names_do_not_break_the_header(self):
        name = report_filename('Al "Mohana"/Home', "2026-07", "report", "pdf")
        assert '"' not in name and "/" not in name
