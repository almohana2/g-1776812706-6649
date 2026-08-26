"""End-to-end over a real socket: the default urllib transport against a
throwaway HTTP server, so the one piece the fake transport cannot cover —
actually speaking HTTP — is exercised too.
"""

import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hydrawise.client import HydrawiseClient

from . import fixtures


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        parsed = urllib.parse.urlparse(self.path)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).requests.append((endpoint, query))

        if query.get("api_key") != "test-key":
            payload = fixtures.ERROR_BAD_KEY
        elif endpoint == "statusschedule.php":
            payload = fixtures.STATUS_SCHEDULE
        elif endpoint == "customerdetails.php":
            payload = fixtures.CUSTOMER_DETAILS
        else:
            payload = fixtures.SETZONE_OK

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output clean
        pass


class LoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}/api/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.requests.clear()

    def client(self, api_key="test-key"):
        return HydrawiseClient(
            api_key, base_url=self.base_url, timeout=5, min_request_interval=0.0
        )

    def test_status_schedule_round_trips_over_http(self):
        status = self.client().status_schedule()
        self.assertEqual(status.name, "Home Controller")
        self.assertEqual(len(status.zones), 3)
        self.assertEqual(_Handler.requests[0][0], "statusschedule.php")

    def test_a_command_round_trips_over_http(self):
        result = self.client().run_zone(100001, 600)
        self.assertTrue(result.ok)
        endpoint, query = _Handler.requests[0]
        self.assertEqual(endpoint, "setzone.php")
        self.assertEqual(query["custom"], "600")

    def test_a_bad_key_is_reported_as_an_auth_error(self):
        from hydrawise.errors import HydrawiseAuthError

        with self.assertRaises(HydrawiseAuthError):
            self.client("wrong-key").customer_details()

    def test_an_unreachable_host_raises_a_connection_error(self):
        from hydrawise.errors import HydrawiseConnectionError

        client = HydrawiseClient(
            "test-key", base_url="http://127.0.0.1:1/api/v1", timeout=2
        )
        with self.assertRaises(HydrawiseConnectionError):
            client.customer_details()


if __name__ == "__main__":
    unittest.main()
