import unittest
from datetime import datetime, timedelta, timezone

from hydrawise.config import Config, ConfigError
from hydrawise.storage import RunRecord
from hydrawise.usage import build_report, month_bounds, previous_month

CONFIG = {
    "currency": "SAR",
    "timezone": "Asia/Riyadh",
    "water": {"tariff_per_m3": 3.0},
    "electricity": {"tariff_per_kwh": 0.2, "default_pump_kw": 2.0},
    "people": [
        {"id": "ahmed", "name": "Ahmed", "email": "ahmed@example.com", "language": "ar"},
        {"id": "sara", "name": "Sara", "email": "sara@example.com"},
    ],
    "zones": [
        {"zone": 1, "relay_id": 100001, "name": "Front lawn", "flow_rate_lpm": 40.0, "owner": "ahmed"},
        {"zone": 2, "relay_id": 100002, "name": "Date palms", "flow_rate_lpm": 60.0, "owner": "sara"},
        {"zone": 3, "relay_id": 100003, "name": "Vegetable beds", "owner": "sara"},
    ],
}

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, tzinfo=timezone.utc)


def run(relay_id, zone, seconds, day=1):
    started = START + timedelta(days=day - 1, hours=6)
    return RunRecord(
        id=None,
        relay_id=relay_id,
        zone_number=zone,
        zone_name=None,
        controller_id=4242,
        started_at=started,
        last_seen_at=started + timedelta(seconds=seconds),
        ended_at=started + timedelta(seconds=seconds),
        seconds=seconds,
        expected_seconds=seconds,
    )


class MonthTests(unittest.TestCase):
    def test_month_bounds_are_local_midnight_in_utc(self):
        start, end = month_bounds("2026-08", "Asia/Riyadh")
        # Riyadh is UTC+3 year-round, so the month starts at 21:00 the day before.
        self.assertEqual(start, datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 31, 21, 0, tzinfo=timezone.utc))

    def test_december_rolls_into_the_next_year(self):
        start, end = month_bounds("2026-12")
        self.assertEqual(end, datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_bad_month_is_rejected(self):
        with self.assertRaises(ValueError):
            month_bounds("2026-13")
        with self.assertRaises(ValueError):
            month_bounds("august")

    def test_previous_month(self):
        self.assertEqual(previous_month(datetime(2026, 1, 4, tzinfo=timezone.utc)), "2025-12")
        self.assertEqual(previous_month(datetime(2026, 8, 26, tzinfo=timezone.utc)), "2026-07")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_dict(CONFIG)

    def build(self, records):
        return build_report(
            records, self.config, period="2026-08", start=START, end=END
        )

    def test_water_volume_uses_flow_rate_times_run_time(self):
        # 30 minutes at 40 L/min = 1200 L = 1.2 m³
        report = self.build([run(100001, 1, 1800)])
        zone = report.person("ahmed").zones[0]
        self.assertAlmostEqual(zone.cubic_meters, 1.2)

    def test_energy_uses_pump_rating_times_run_time(self):
        # 30 minutes at 2 kW = 1 kWh
        report = self.build([run(100001, 1, 1800)])
        zone = report.person("ahmed").zones[0]
        self.assertAlmostEqual(zone.kwh, 1.0)

    def test_costs_apply_both_tariffs(self):
        report = self.build([run(100001, 1, 1800)])
        person = report.person("ahmed")
        self.assertAlmostEqual(person.water_cost, 3.6)  # 1.2 m³ × 3.0
        self.assertAlmostEqual(person.energy_cost, 0.2)  # 1 kWh × 0.2
        self.assertAlmostEqual(person.total_cost, 3.8)

    def test_runs_are_summed_per_zone_and_per_person(self):
        report = self.build(
            [
                run(100001, 1, 1800, day=1),
                run(100001, 1, 1800, day=2),
                run(100002, 2, 3600, day=2),
            ]
        )
        ahmed = report.person("ahmed")
        sara = report.person("sara")
        self.assertEqual(ahmed.zones[0].runs, 2)
        self.assertEqual(ahmed.seconds, 3600)
        self.assertAlmostEqual(ahmed.cubic_meters, 2.4)
        self.assertAlmostEqual(sara.cubic_meters, 3.6)  # 60 min × 60 L/min

    def test_a_zone_without_a_flow_rate_reports_time_but_no_volume(self):
        report = self.build([run(100003, 3, 1800)])
        zone = next(z for z in report.person("sara").zones if z.name == "Vegetable beds")
        self.assertEqual(zone.seconds, 1800)
        self.assertIsNone(zone.cubic_meters)
        self.assertAlmostEqual(zone.kwh, 1.0)
        self.assertTrue(any("flow rate" in warning for warning in report.warnings))

    def test_zones_follow_the_config_order(self):
        report = self.build([run(100003, 3, 600)])
        self.assertEqual(
            [zone.name for zone in report.person("sara").zones],
            ["Date palms", "Vegetable beds"],
        )

    def test_configured_zones_with_no_runs_still_appear(self):
        report = self.build([])
        self.assertEqual(len(report.person("ahmed").zones), 1)
        self.assertEqual(report.person("ahmed").seconds, 0)
        self.assertEqual(report.person("ahmed").total_cost, 0.0)

    def test_an_unconfigured_zone_lands_in_unassigned(self):
        report = self.build([run(100009, 9, 600)])
        self.assertEqual(len(report.unassigned), 1)
        self.assertEqual(report.unassigned[0].seconds, 600)
        self.assertTrue(any("Unassigned" in warning for warning in report.warnings))

    def test_site_totals_add_up(self):
        report = self.build([run(100001, 1, 1800), run(100002, 2, 1800)])
        self.assertAlmostEqual(report.cubic_meters, 1.2 + 1.8)
        self.assertAlmostEqual(report.kwh, 2.0)
        self.assertEqual(report.seconds, 3600)

    def test_share_of_the_site_total(self):
        report = self.build([run(100001, 1, 1800), run(100002, 2, 1800)])
        ahmed = report.person("ahmed")
        self.assertAlmostEqual(
            ahmed.share_of(report.cubic_meters, ahmed.cubic_meters), 40.0
        )


class ConfigTests(unittest.TestCase):
    def test_zone_owned_by_an_unknown_person_is_rejected(self):
        payload = {**CONFIG, "zones": [{"zone": 1, "owner": "nobody"}]}
        with self.assertRaises(ConfigError):
            Config.from_dict(payload)

    def test_duplicate_person_ids_are_rejected(self):
        payload = {
            **CONFIG,
            "people": [{"id": "ahmed"}, {"id": "ahmed"}],
            "zones": [],
        }
        with self.assertRaises(ConfigError):
            Config.from_dict(payload)

    def test_a_zone_needs_an_identifier(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({**CONFIG, "zones": [{"name": "nameless"}], "people": []})

    def test_zone_lookup_prefers_relay_id(self):
        config = Config.from_dict(CONFIG)
        self.assertEqual(config.zone_for(100002, None).owner, "sara")
        self.assertEqual(config.zone_for(None, 1).owner, "ahmed")
        self.assertIsNone(config.zone_for(999999, None))

    def test_pump_rating_falls_back_to_the_site_default(self):
        config = Config.from_dict(CONFIG)
        self.assertEqual(config.pump_kw_for(config.zones[2]), 2.0)

    def test_secrets_come_from_the_environment_not_the_file(self):
        config = Config.from_dict(CONFIG)
        self.assertEqual(config.api_key_env, "HYDRAWISE_API_KEY")
        self.assertEqual(config.email.password_env, "HYDRAWISE_SMTP_PASSWORD")


if __name__ == "__main__":
    unittest.main()
