import csv
import io
import json
import unittest
from datetime import datetime, timezone

from hydrawise.config import Config
from hydrawise.report import (
    format_duration,
    render_csv,
    render_person_html,
    render_person_text,
    render_summary,
    to_dict,
)
from hydrawise.usage import build_report

from .test_usage import CONFIG, END, START, run


def sample_report():
    config = Config.from_dict(CONFIG)
    records = [run(100001, 1, 1800, day=1), run(100002, 2, 3600, day=2)]
    return build_report(
        records,
        config,
        period="2026-08",
        start=START,
        end=END,
        generated_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


class DurationTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_duration(9015), "2h 30m")
        self.assertEqual(format_duration(150), "2m 30s")
        self.assertEqual(format_duration(9), "9s")
        self.assertEqual(format_duration(-5), "0s")


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()

    def test_summary_lists_every_person_and_the_total(self):
        text = render_summary(self.report)
        self.assertIn("Ahmed", text)
        self.assertIn("Sara", text)
        self.assertIn("Front lawn", text)
        self.assertIn("2026-08", text)
        self.assertIn("TOTAL", text.upper())

    def test_person_text_is_arabic_when_configured(self):
        ahmed = self.report.person("ahmed")
        text = render_person_text(ahmed, self.report)
        self.assertIn("مرحباً", text)
        self.assertIn("Front lawn", text)

    def test_person_text_defaults_to_english(self):
        sara = self.report.person("sara")
        text = render_person_text(sara, self.report)
        self.assertIn("Hello Sara", text)
        self.assertIn("Irrigation", render_summary(self.report))

    def test_person_text_says_so_when_nothing_ran(self):
        report = build_report([], Config.from_dict(CONFIG), period="2026-08", start=START, end=END)
        text = render_person_text(report.person("sara"), report)
        self.assertIn("No watering was recorded", text)

    def test_html_is_right_to_left_for_arabic(self):
        html = render_person_html(self.report.person("ahmed"), self.report)
        self.assertIn('dir="rtl"', html)
        self.assertIn('lang="ar"', html)
        self.assertIn("<table", html)

    def test_html_escapes_zone_names(self):
        config = dict(CONFIG)
        config["zones"] = [
            {"zone": 1, "relay_id": 100001, "name": "<script>x</script>", "flow_rate_lpm": 10.0, "owner": "sara"}
        ]
        report = build_report(
            [run(100001, 1, 600)], Config.from_dict(config), period="2026-08", start=START, end=END
        )
        html = render_person_html(report.person("sara"), report)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_csv_has_one_row_per_zone(self):
        rows = list(csv.DictReader(io.StringIO(render_csv(self.report))))
        self.assertEqual(len(rows), 3)  # three configured zones
        front = next(row for row in rows if row["zone"] == "Front lawn")
        self.assertEqual(front["person_id"], "ahmed")
        self.assertEqual(front["seconds"], "1800")
        self.assertAlmostEqual(float(front["cubic_meters"]), 1.2)
        self.assertEqual(front["currency"], "SAR")

    def test_json_round_trips(self):
        payload = json.loads(json.dumps(to_dict(self.report)))
        self.assertEqual(payload["period"], "2026-08")
        self.assertAlmostEqual(payload["totals"]["cubic_meters"], 4.8)
        ahmed = next(item for item in payload["people"] if item["id"] == "ahmed")
        self.assertAlmostEqual(ahmed["cubic_meters"], 1.2)
        self.assertEqual(ahmed["zones"][0]["runs"], 1)

    def test_zones_without_a_flow_rate_render_as_unknown_not_zero(self):
        report = build_report(
            [run(100003, 3, 1800)], Config.from_dict(CONFIG), period="2026-08", start=START, end=END
        )
        text = render_person_text(report.person("sara"), report)
        self.assertIn("—", text)


if __name__ == "__main__":
    unittest.main()
