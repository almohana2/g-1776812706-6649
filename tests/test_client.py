import unittest

from hydrawise.client import CUSTOM_PERIOD_ID, HttpResponse, HydrawiseClient
from hydrawise.errors import (
    HydrawiseAPIError,
    HydrawiseAuthError,
    HydrawiseRateLimitError,
)

from . import fixtures
from .support import FakeClock, FakeTransport, make_client


class RequestBuildingTests(unittest.TestCase):
    def test_customer_details_sends_api_key_and_type(self):
        transport = FakeTransport.json(fixtures.CUSTOMER_DETAILS)
        client = make_client(transport)

        details = client.customer_details()

        self.assertEqual(transport.endpoint(), "customerdetails.php")
        self.assertEqual(transport.query()["api_key"], "test-key")
        self.assertEqual(transport.query()["type"], "controllers")
        self.assertEqual(details.customer_id, 1337)
        self.assertEqual(len(details.controllers), 2)
        self.assertEqual(details.controllers[0].name, "Home Controller")

    def test_status_schedule_passes_controller_id(self):
        transport = FakeTransport.json(fixtures.STATUS_SCHEDULE)
        client = make_client(transport)

        client.status_schedule(4242)

        self.assertEqual(transport.endpoint(), "statusschedule.php")
        self.assertEqual(transport.query()["controller_id"], "4242")

    def test_run_zone_sends_custom_duration(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        client = make_client(transport)

        result = client.run_zone(100001, 1800)

        query = transport.query()
        self.assertEqual(transport.endpoint(), "setzone.php")
        self.assertEqual(query["action"], "run")
        self.assertEqual(query["relay_id"], "100001")
        self.assertEqual(query["custom"], "1800")
        self.assertEqual(query["period_id"], str(CUSTOM_PERIOD_ID))
        self.assertTrue(result.ok)

    def test_run_zone_without_duration_omits_custom(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        client = make_client(transport)

        client.run_zone(100001)

        self.assertNotIn("custom", transport.query())
        self.assertNotIn("period_id", transport.query())

    def test_stop_all_takes_no_relay(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        client = make_client(transport)

        client.stop_all_zones()

        self.assertEqual(transport.query()["action"], "stopall")
        self.assertNotIn("relay_id", transport.query())

    def test_resume_zone_suspends_with_zero(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        client = make_client(transport)

        client.resume_zone(100002)

        query = transport.query()
        self.assertEqual(query["action"], "suspend")
        self.assertEqual(query["custom"], "0")

    def test_suspend_zone_converts_offset_to_epoch(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        client = make_client(transport)

        client.suspend_zone(100002, 86400)

        self.assertGreater(int(transport.query()["custom"]), 1_600_000_000)

    def test_all_actions_reject_a_relay_id(self):
        client = make_client(FakeTransport.json(fixtures.SETZONE_OK))
        with self.assertRaises(ValueError):
            client._set_zone("runall", relay_id=1)

    def test_single_zone_actions_require_a_relay_id(self):
        client = make_client(FakeTransport.json(fixtures.SETZONE_OK))
        with self.assertRaises(ValueError):
            client._set_zone("run")

    def test_empty_api_key_is_rejected(self):
        with self.assertRaises(HydrawiseAuthError):
            HydrawiseClient("   ")


class ErrorHandlingTests(unittest.TestCase):
    def test_error_msg_about_the_key_raises_auth_error(self):
        client = make_client(FakeTransport.json(fixtures.ERROR_BAD_KEY))
        with self.assertRaises(HydrawiseAuthError) as caught:
            client.customer_details()
        self.assertIn("Invalid API key", str(caught.exception))

    def test_error_msg_about_the_limit_raises_rate_limit_error(self):
        client = make_client(FakeTransport.json(fixtures.ERROR_RATE_LIMIT))
        with self.assertRaises(HydrawiseRateLimitError):
            client.customer_details()

    def test_http_429_raises_rate_limit_error_after_retries(self):
        clock = FakeClock()
        transport = FakeTransport(
            [HttpResponse(429, "slow down", {"Retry-After": "5"})]
        )
        client = make_client(transport, clock, max_retries=1)

        with self.assertRaises(HydrawiseRateLimitError) as caught:
            client.customer_details()

        self.assertEqual(caught.exception.retry_after, 5.0)
        self.assertEqual(transport.call_count, 2)  # one try, one retry
        self.assertEqual(clock.slept, [5.0])

    def test_transient_500_is_retried_then_succeeds(self):
        transport = FakeTransport(
            [
                HttpResponse(503, "nope", {}),
                HttpResponse(200, fixtures.body(fixtures.CUSTOMER_DETAILS), {}),
            ]
        )
        client = make_client(transport, max_retries=2)

        details = client.customer_details()

        self.assertEqual(details.customer_id, 1337)
        self.assertEqual(transport.call_count, 2)

    def test_non_json_body_raises_api_error(self):
        client = make_client(FakeTransport([HttpResponse(200, "<html>oops</html>", {})]))
        with self.assertRaises(HydrawiseAPIError):
            client.customer_details()

    def test_api_key_is_not_leaked_in_error_messages(self):
        client = make_client(FakeTransport([HttpResponse(200, "not json", {})]))
        with self.assertRaises(HydrawiseAPIError) as caught:
            client.customer_details()
        self.assertNotIn("test-key", str(caught.exception))
        self.assertIn("***", str(caught.exception))


class ThrottlingTests(unittest.TestCase):
    def test_status_schedule_is_cached_until_nextpoll_elapses(self):
        clock = FakeClock()
        transport = FakeTransport.json(fixtures.STATUS_SCHEDULE)
        client = make_client(transport, clock)

        client.status_schedule()
        client.status_schedule()
        self.assertEqual(transport.call_count, 1)

        clock.advance(61)
        client.status_schedule()
        self.assertEqual(transport.call_count, 2)

    def test_force_bypasses_the_cache(self):
        transport = FakeTransport.json(fixtures.STATUS_SCHEDULE)
        client = make_client(transport)

        client.status_schedule()
        client.status_schedule(force=True)

        self.assertEqual(transport.call_count, 2)

    def test_next_status_poll_in_counts_down(self):
        clock = FakeClock()
        client = make_client(FakeTransport.json(fixtures.STATUS_SCHEDULE), clock)

        self.assertEqual(client.next_status_poll_in(), 0.0)
        client.status_schedule()
        self.assertEqual(client.next_status_poll_in(), 60.0)
        clock.advance(45)
        self.assertEqual(client.next_status_poll_in(), 15.0)

    def test_a_command_invalidates_the_status_cache(self):
        transport = FakeTransport(
            [
                HttpResponse(200, fixtures.body(fixtures.STATUS_SCHEDULE), {}),
                HttpResponse(200, fixtures.body(fixtures.SETZONE_OK), {}),
                HttpResponse(200, fixtures.body(fixtures.STATUS_SCHEDULE), {}),
            ]
        )
        client = make_client(transport)

        client.status_schedule()
        client.stop_zone(100001)
        client.status_schedule()

        self.assertEqual(transport.call_count, 3)

    def test_min_request_interval_spaces_requests_out(self):
        clock = FakeClock()
        transport = FakeTransport.json(fixtures.CUSTOMER_DETAILS)
        client = make_client(transport, clock, min_request_interval=2.0)

        client.customer_details()
        client.customer_details()

        self.assertEqual(clock.slept, [2.0])


if __name__ == "__main__":
    unittest.main()
