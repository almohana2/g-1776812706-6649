import unittest

from hydrawise.config import Config
from hydrawise.mailer import Mailer, build_message, send_reports

from .test_report import sample_report
from .test_usage import CONFIG


class RecordingMailer(Mailer):
    def __init__(self, config, fail_for=()):
        super().__init__(config, sender=self._record)
        self.sent = []
        self.fail_for = set(fail_for)

    def _record(self, message):
        if message["To"] in self.fail_for:
            raise RuntimeError("mailbox full")
        self.sent.append(message)


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()
        self.config = Config.from_dict(CONFIG).email

    def test_message_has_both_a_text_and_an_html_part(self):
        message = build_message(self.report, self.report.person("sara"), self.config)
        self.assertEqual(message["To"], "sara@example.com")
        types = {part.get_content_type() for part in message.walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)

    def test_subject_carries_the_period_and_the_name(self):
        message = build_message(self.report, self.report.person("ahmed"), self.config)
        self.assertIn("2026-08", message["Subject"])
        self.assertIn("Ahmed", message["Subject"])

    def test_a_person_without_an_email_cannot_be_addressed(self):
        report = sample_report()
        report.people[0].person.email = None
        with self.assertRaises(ValueError):
            build_message(report, report.people[0], self.config)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()
        self.config = Config.from_dict(CONFIG).email

    def test_everyone_gets_one_message(self):
        mailer = RecordingMailer(self.config)
        results = send_reports(self.report, self.config, mailer=mailer)
        self.assertEqual([result.status for result in results], ["sent", "sent"])
        self.assertEqual(len(mailer.sent), 2)

    def test_dry_run_sends_nothing(self):
        mailer = RecordingMailer(self.config)
        results = send_reports(self.report, self.config, mailer=mailer, dry_run=True)
        self.assertEqual(mailer.sent, [])
        self.assertTrue(all(result.status == "skipped" for result in results))

    def test_only_limits_the_recipients(self):
        mailer = RecordingMailer(self.config)
        results = send_reports(self.report, self.config, mailer=mailer, only=["sara"])
        self.assertEqual([result.person_id for result in results], ["sara"])

    def test_a_failure_does_not_stop_the_rest(self):
        mailer = RecordingMailer(self.config, fail_for={"ahmed@example.com"})
        results = send_reports(self.report, self.config, mailer=mailer)
        statuses = {result.person_id: result.status for result in results}
        self.assertEqual(statuses["ahmed"], "failed")
        self.assertEqual(statuses["sara"], "sent")
        self.assertEqual(len(mailer.sent), 1)

    def test_people_without_an_address_are_skipped(self):
        self.report.people[0].person.email = None
        mailer = RecordingMailer(self.config)
        results = send_reports(self.report, self.config, mailer=mailer)
        self.assertEqual(results[0].status, "skipped")
        self.assertIn("no email address", results[0].detail)

    def test_skip_empty_suppresses_zero_usage_mail(self):
        from hydrawise.usage import build_report
        from .test_usage import CONFIG as raw_config, END, START

        report = build_report([], Config.from_dict(raw_config), period="2026-08", start=START, end=END)
        mailer = RecordingMailer(self.config)
        results = send_reports(report, self.config, mailer=mailer, skip_empty=True)
        self.assertEqual(mailer.sent, [])
        self.assertTrue(all("no usage" in result.detail for result in results))


if __name__ == "__main__":
    unittest.main()
