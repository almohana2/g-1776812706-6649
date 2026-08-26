import unittest
from datetime import datetime, timedelta, timezone

from hydrawise.client import HttpResponse
from hydrawise.errors import HydrawiseRateLimitError
from hydrawise.poller import poll_forever, poll_once
from hydrawise.storage import RunStore

from . import fixtures
from .support import FakeTransport, make_client

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


class PollTests(unittest.TestCase):
    def setUp(self):
        self.store = RunStore(":memory:")
        self.addCleanup(self.store.close)

    def test_poll_once_records_the_running_zone(self):
        client = make_client(FakeTransport.json(fixtures.STATUS_SCHEDULE))

        events = poll_once(client, self.store, now=T0)

        self.assertEqual([event.kind for event in events], ["started"])
        self.assertEqual(self.store.open_runs()[0].zone_name, "Front lawn")

    def test_the_loop_bypasses_the_status_cache(self):
        # Without force=True the client would serve the cached response and the
        # log would never see the run end.
        transport = FakeTransport(
            [
                HttpResponse(200, fixtures.body(fixtures.STATUS_SCHEDULE), {}),
                HttpResponse(200, fixtures.body(fixtures.STATUS_SCHEDULE_IDLE), {}),
            ]
        )
        client = make_client(transport)
        moments = iter([T0, T0 + timedelta(minutes=20)])
        slept = []

        poll_forever(
            client,
            self.store,
            interval=60,
            sleep=slept.append,
            now=lambda: next(moments),
            max_iterations=2,
        )

        self.assertEqual(transport.call_count, 2)
        runs = self.store.all_runs()
        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0].is_open)

    def test_a_rate_limit_widens_the_interval_without_ending_the_loop(self):
        transport = FakeTransport(
            [HttpResponse(429, "slow down", {"Retry-After": "120"})]
        )
        client = make_client(transport, max_retries=0)
        outcomes = []
        slept = []

        iterations = poll_forever(
            client,
            self.store,
            interval=60,
            sleep=slept.append,
            now=lambda: T0,
            on_outcome=outcomes.append,
            max_iterations=2,
        )

        self.assertEqual(iterations, 2)
        self.assertTrue(all(isinstance(o.error, HydrawiseRateLimitError) for o in outcomes))
        self.assertEqual(slept, [120.0])

    def test_stop_predicate_ends_the_loop_before_the_next_sleep(self):
        client = make_client(FakeTransport.json(fixtures.STATUS_SCHEDULE))
        outcomes = []
        slept = []

        iterations = poll_forever(
            client,
            self.store,
            interval=60,
            sleep=slept.append,
            now=lambda: T0,
            on_outcome=outcomes.append,
            stop=lambda: len(outcomes) >= 2,
        )

        self.assertEqual(iterations, 2)
        # The loop checks again after each poll, so shutdown does not wait out
        # a full interval.
        self.assertEqual(slept, [60])


if __name__ == "__main__":
    unittest.main()
