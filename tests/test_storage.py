import unittest
from datetime import datetime, timedelta, timezone

from hydrawise.models import StatusSchedule
from hydrawise.storage import RunStore

from . import fixtures

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


def status_with_running(running):
    payload = dict(fixtures.STATUS_SCHEDULE)
    payload["running"] = running
    return StatusSchedule.from_api(payload)


RUNNING_FRONT = [
    {"relay_id": 100001, "relay": 1, "name": "Front lawn", "time_left": 1800, "run": 1800}
]
RUNNING_FRONT_HALFWAY = [
    {"relay_id": 100001, "relay": 1, "name": "Front lawn", "time_left": 900, "run": 1800}
]
RUNNING_PALMS = [
    {"relay_id": 100002, "relay": 2, "name": "Date palms", "time_left": 2700, "run": 2700}
]
IDLE = []


class RunTrackingTests(unittest.TestCase):
    def setUp(self):
        self.store = RunStore(":memory:")
        self.addCleanup(self.store.close)

    def test_a_run_opens_and_closes_across_polls(self):
        events = self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.assertEqual([event.kind for event in events], ["started"])

        self.store.record_status(
            status_with_running(RUNNING_FRONT_HALFWAY), now=T0 + timedelta(minutes=15)
        )
        events = self.store.record_status(
            status_with_running(IDLE), now=T0 + timedelta(minutes=31)
        )
        self.assertEqual([event.kind for event in events], ["finished"])

        runs = self.store.all_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].zone_name, "Front lawn")
        self.assertEqual(runs[0].seconds, 1800)  # the full programmed run
        self.assertFalse(runs[0].is_open)

    def test_a_run_stopped_early_only_counts_observed_time(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.store.record_status(
            status_with_running(RUNNING_FRONT_HALFWAY), now=T0 + timedelta(minutes=10)
        )
        self.store.record_status(status_with_running(IDLE), now=T0 + timedelta(minutes=11))

        run = self.store.all_runs()[0]
        self.assertEqual(run.seconds, 660)  # 11 minutes, not the programmed 30

    def test_starting_mid_run_backdates_the_start(self):
        self.store.record_status(status_with_running(RUNNING_FRONT_HALFWAY), now=T0)

        run = self.store.all_runs()[0]
        self.assertEqual(run.started_at, T0 - timedelta(minutes=15))

    def test_two_zones_are_tracked_independently(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.store.record_status(
            status_with_running(RUNNING_PALMS), now=T0 + timedelta(minutes=31)
        )
        self.store.record_status(
            status_with_running(IDLE), now=T0 + timedelta(minutes=76)
        )

        runs = self.store.all_runs()
        self.assertEqual([run.zone_name for run in runs], ["Front lawn", "Date palms"])
        self.assertEqual([run.seconds for run in runs], [1800, 2700])

    def test_an_open_run_is_still_counted(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.store.record_status(
            status_with_running(RUNNING_FRONT_HALFWAY), now=T0 + timedelta(minutes=15)
        )

        open_runs = self.store.open_runs()
        self.assertEqual(len(open_runs), 1)
        self.assertEqual(open_runs[0].seconds, 900)

    def test_close_stale_ends_runs_the_poller_lost_sight_of(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)

        closed = self.store.close_stale(now=T0 + timedelta(hours=6), max_gap_seconds=900)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].seconds, 1800)
        self.assertEqual(self.store.open_runs(), [])

    def test_close_stale_without_a_programmed_length_counts_only_what_was_seen(self):
        running = [{"relay_id": 100001, "relay": 1, "name": "Front lawn"}]
        self.store.record_status(status_with_running(running), now=T0)
        self.store.record_status(
            status_with_running(running), now=T0 + timedelta(minutes=5)
        )

        closed = self.store.close_stale(now=T0 + timedelta(hours=6), max_gap_seconds=900)

        self.assertEqual(closed[0].seconds, 300)

    def test_close_stale_leaves_a_freshly_seen_run_alone(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)

        closed = self.store.close_stale(now=T0 + timedelta(minutes=5), max_gap_seconds=900)

        self.assertEqual(closed, [])
        self.assertEqual(len(self.store.open_runs()), 1)

    def test_runs_between_filters_by_start(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.store.record_status(status_with_running(IDLE), now=T0 + timedelta(hours=1))
        later = T0 + timedelta(days=40)
        self.store.record_status(status_with_running(RUNNING_PALMS), now=later)
        self.store.record_status(
            status_with_running(IDLE), now=later + timedelta(hours=1)
        )

        august = self.store.runs_between(
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertEqual([run.zone_name for run in august], ["Front lawn"])

    def test_polls_are_recorded(self):
        self.store.record_status(status_with_running(IDLE), now=T0)
        self.store.record_status(status_with_running(IDLE), now=T0 + timedelta(minutes=1))

        self.assertEqual(self.store.poll_count(), 2)
        self.assertEqual(self.store.last_poll_at(), T0 + timedelta(minutes=1))

    def test_zone_names_are_summarised(self):
        self.store.record_status(status_with_running(RUNNING_FRONT), now=T0)
        self.assertEqual(self.store.zone_names(), [(100001, 1, "Front lawn")])


if __name__ == "__main__":
    unittest.main()
