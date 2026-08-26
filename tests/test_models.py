import unittest
from datetime import datetime, timezone

from hydrawise.models import NEVER_SECONDS, CustomerDetails, StatusSchedule

from . import fixtures


class StatusScheduleTests(unittest.TestCase):
    def setUp(self):
        self.status = StatusSchedule.from_api(fixtures.STATUS_SCHEDULE)

    def test_parses_zones_sensors_and_runs(self):
        self.assertEqual(len(self.status.zones), 3)
        self.assertEqual(len(self.status.sensors), 1)
        self.assertEqual(len(self.status.running), 1)
        self.assertEqual(self.status.next_poll, 60)
        self.assertEqual(self.status.sensors[0].relay_ids, [100001, 100002])

    def test_numeric_strings_are_coerced(self):
        # The API sends "run" as a string; the model exposes an int.
        self.assertEqual(self.status.zones[0].next_run_seconds, 1800)

    def test_next_run_at_is_relative_to_server_time(self):
        zone = self.status.zones[0]
        expected = datetime.fromtimestamp(1755000000 + 3600, tz=timezone.utc)
        self.assertEqual(zone.next_run_at, expected)

    def test_the_never_placeholder_means_not_scheduled(self):
        palms = self.status.zones[1]
        self.assertEqual(palms.seconds_until_next_run, NEVER_SECONDS)
        self.assertFalse(palms.is_scheduled)
        self.assertTrue(palms.is_suspended)
        self.assertIsNone(palms.next_run_at)

    def test_running_zone_exposes_time_remaining(self):
        running = self.status.running_zone(100001)
        self.assertIsNotNone(running)
        self.assertEqual(running.time_remaining.total_seconds(), 600)
        self.assertTrue(self.status.is_running(100001))
        self.assertFalse(self.status.is_running(100002))

    def test_lookup_by_relay_id(self):
        self.assertEqual(self.status.zone(100002).name, "Date palms")

    def test_lookup_by_zone_number(self):
        self.assertEqual(self.status.zone(3).name, "Vegetable beds")

    def test_lookup_by_name_is_case_insensitive(self):
        self.assertEqual(self.status.zone("front lawn").relay_id, 100001)

    def test_lookup_by_unique_substring(self):
        self.assertEqual(self.status.zone("palms").relay_id, 100002)

    def test_lookup_of_an_unknown_zone_returns_none(self):
        self.assertIsNone(self.status.zone("orchard"))
        self.assertIsNone(self.status.zone(99))

    def test_raw_payload_is_preserved(self):
        self.assertEqual(self.status.zones[0].raw["type"], 106)

    def test_missing_arrays_do_not_break_parsing(self):
        status = StatusSchedule.from_api({"controller_id": 1})
        self.assertEqual(status.zones, [])
        self.assertEqual(status.running, [])
        self.assertIsNone(status.server_time)


class CustomerDetailsTests(unittest.TestCase):
    def test_parses_controllers(self):
        details = CustomerDetails.from_api(fixtures.CUSTOMER_DETAILS)
        self.assertEqual(details.controller_id, 4242)
        self.assertEqual(details.controller(4243).name, "Farm Controller")
        self.assertTrue(details.controllers[0].online)
        self.assertFalse(details.controllers[1].online)
        self.assertEqual(
            details.controllers[0].last_contact,
            datetime.fromtimestamp(1755000000, tz=timezone.utc),
        )

    def test_unknown_controller_lookup_returns_none(self):
        details = CustomerDetails.from_api(fixtures.CUSTOMER_DETAILS)
        self.assertIsNone(details.controller(1))


if __name__ == "__main__":
    unittest.main()
