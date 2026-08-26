import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hydrawise import cli
from hydrawise.storage import RunRecord, RunStore

from . import fixtures
from .support import FakeTransport, make_client
from .test_usage import CONFIG

T0 = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)


def seed_runs(db_path):
    with RunStore(db_path) as store:
        store.add_runs(
            [
                RunRecord(
                    id=None,
                    relay_id=100001,
                    zone_number=1,
                    zone_name="Front lawn",
                    controller_id=4242,
                    started_at=T0,
                    last_seen_at=T0 + timedelta(seconds=1800),
                    ended_at=T0 + timedelta(seconds=1800),
                    seconds=1800,
                    expected_seconds=1800,
                ),
                RunRecord(
                    id=None,
                    relay_id=100002,
                    zone_number=2,
                    zone_name="Date palms",
                    controller_id=4242,
                    started_at=T0 + timedelta(days=1),
                    last_seen_at=T0 + timedelta(days=1, seconds=3600),
                    ended_at=T0 + timedelta(days=1, seconds=3600),
                    seconds=3600,
                    expected_seconds=3600,
                ),
            ]
        )


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "runs.db")
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
        self.out = io.StringIO()

    def run_cli(self, *args, transport=None):
        argv = ["--config", str(self.config_path), "--db", self.db, *args]
        if transport is None:
            return cli.main(argv, out=self.out)
        client = make_client(transport)
        with mock.patch.object(cli, "_client", return_value=client):
            return cli.main(argv, out=self.out)

    @property
    def output(self):
        return self.out.getvalue()


class LocalCommandTests(CliTestCase):
    def test_init_config_writes_a_starter_file(self):
        target = self.root / "new.json"
        code = cli.main(["--config", str(target), "init-config"], out=self.out)
        self.assertEqual(code, 0)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("zones", payload)
        self.assertIn("HYDRAWISE_API_KEY", payload["api_key_env"])

    def test_init_config_refuses_to_overwrite(self):
        code = cli.main(["--config", str(self.config_path), "init-config"], out=self.out)
        self.assertEqual(code, 1)
        self.assertIn("already exists", self.output)

    def test_runs_reports_an_empty_log(self):
        self.assertEqual(self.run_cli("runs"), 0)
        self.assertIn("No runs logged yet", self.output)

    def test_runs_lists_logged_runs(self):
        seed_runs(self.db)
        self.assertEqual(self.run_cli("runs"), 0)
        self.assertIn("Front lawn", self.output)
        self.assertIn("30m", self.output)

    def test_report_text_shows_every_person(self):
        seed_runs(self.db)
        self.assertEqual(self.run_cli("report", "--month", "2026-08"), 0)
        self.assertIn("Ahmed", self.output)
        self.assertIn("Sara", self.output)
        self.assertIn("2026-08", self.output)

    def test_report_json_has_totals(self):
        seed_runs(self.db)
        self.assertEqual(self.run_cli("report", "--month", "2026-08", "--format", "json"), 0)
        payload = json.loads(self.output)
        self.assertAlmostEqual(payload["totals"]["cubic_meters"], 1.2 + 3.6)

    def test_report_csv_is_parseable(self):
        seed_runs(self.db)
        self.assertEqual(self.run_cli("report", "--month", "2026-08", "--format", "csv"), 0)
        self.assertIn("person_id", self.output.splitlines()[0])

    def test_report_html_needs_a_person(self):
        seed_runs(self.db)
        self.assertEqual(self.run_cli("report", "--month", "2026-08", "--format", "html"), 1)

    def test_report_html_renders_one_persons_mail(self):
        seed_runs(self.db)
        code = self.run_cli(
            "report", "--month", "2026-08", "--format", "html", "--person", "ahmed"
        )
        self.assertEqual(code, 0)
        self.assertIn("<table", self.output)
        self.assertIn('dir="rtl"', self.output)

    def test_report_writes_to_a_file(self):
        seed_runs(self.db)
        target = self.root / "august.csv"
        code = self.run_cli(
            "report", "--month", "2026-08", "--format", "csv", "--output", str(target)
        )
        self.assertEqual(code, 0)
        self.assertIn("Front lawn", target.read_text(encoding="utf-8"))

    def test_report_needs_a_config_file(self):
        code = cli.main(
            ["--config", str(self.root / "missing.json"), "--db", self.db, "report"],
            out=self.out,
        )
        self.assertEqual(code, 1)

    def test_send_reports_dry_run_sends_nothing(self):
        seed_runs(self.db)
        code = self.run_cli("send-reports", "--month", "2026-08", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("ahmed", self.output)
        self.assertIn("dry run", self.output)

    def test_send_reports_refuses_without_smtp_settings(self):
        seed_runs(self.db)
        code = self.run_cli("send-reports", "--month", "2026-08")
        self.assertEqual(code, 1)


class LiveCommandTests(CliTestCase):
    def test_controllers_lists_the_account(self):
        code = self.run_cli("controllers", transport=FakeTransport.json(fixtures.CUSTOMER_DETAILS))
        self.assertEqual(code, 0)
        self.assertIn("Home Controller", self.output)
        self.assertIn("4242", self.output)

    def test_status_shows_zones_and_live_runs(self):
        code = self.run_cli("status", transport=FakeTransport.json(fixtures.STATUS_SCHEDULE))
        self.assertEqual(code, 0)
        self.assertIn("Front lawn", self.output)
        self.assertIn("watering now", self.output)
        self.assertIn("not scheduled", self.output)

    def test_status_json_prints_the_raw_payload(self):
        argv = ["--config", str(self.config_path), "--db", self.db, "--json", "status"]
        client = make_client(FakeTransport.json(fixtures.STATUS_SCHEDULE))
        with mock.patch.object(cli, "_client", return_value=client):
            code = cli.main(argv, out=self.out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.output)["controller_id"], 4242)

    def test_run_resolves_a_zone_by_name(self):
        transport = FakeTransport.json(fixtures.STATUS_SCHEDULE, fixtures.SETZONE_OK)
        code = self.run_cli("run", "palms", "--minutes", "10", transport=transport)
        self.assertEqual(code, 0)
        query = transport.query()
        self.assertEqual(query["relay_id"], "100002")
        self.assertEqual(query["custom"], "600")

    def test_run_rejects_an_unknown_zone(self):
        code = self.run_cli("run", "orchard", transport=FakeTransport.json(fixtures.STATUS_SCHEDULE))
        self.assertEqual(code, 1)

    def test_run_needs_a_zone_or_all(self):
        code = self.run_cli("run", transport=FakeTransport.json(fixtures.STATUS_SCHEDULE))
        self.assertEqual(code, 1)

    def test_stop_all(self):
        transport = FakeTransport.json(fixtures.SETZONE_OK)
        code = self.run_cli("stop", "--all", transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.query()["action"], "stopall")

    def test_suspend_needs_a_deadline(self):
        code = self.run_cli("suspend", "1", transport=FakeTransport.json(fixtures.SETZONE_OK))
        self.assertEqual(code, 1)

    def test_suspend_with_days(self):
        transport = FakeTransport.json(fixtures.STATUS_SCHEDULE, fixtures.SETZONE_OK)
        code = self.run_cli("suspend", "1", "--days", "3", transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(transport.query()["action"], "suspend")

    def test_poll_once_writes_to_the_log(self):
        code = self.run_cli("poll", "--once", transport=FakeTransport.json(fixtures.STATUS_SCHEDULE))
        self.assertEqual(code, 0)
        with RunStore(self.db) as store:
            self.assertEqual(len(store.all_runs()), 1)


class ApiKeyTests(CliTestCase):
    def test_a_missing_api_key_is_reported_clearly(self):
        env = {key: value for key, value in os.environ.items() if key != "HYDRAWISE_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            code = cli.main(
                ["--config", str(self.config_path), "--db", self.db, "controllers"],
                out=self.out,
            )
        self.assertEqual(code, 1)

    def test_the_env_var_named_by_the_config_is_used(self):
        with mock.patch.dict(os.environ, {"HYDRAWISE_API_KEY": "from-env"}, clear=False):
            from hydrawise.config import Config

            config = Config.from_dict(CONFIG)
            args = cli.build_parser().parse_args(["controllers"])
            self.assertEqual(cli._resolve_api_key(args, config), "from-env")


if __name__ == "__main__":
    unittest.main()
