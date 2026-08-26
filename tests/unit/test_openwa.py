"""إرسال واتساب: الحماية من التكرار، إعادة المحاولة، وإخفاء الرقم (SRS §15، AC-010)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from app.core.config import get_settings, reset_settings_cache
from app.models import (
    Controller,
    DeliveryStatus,
    MonthlyReport,
    NotificationDelivery,
    ReportStatus,
)
from app.services.openwa_client import (
    OpenWAClient,
    OpenWAError,
    _chat_id,
    build_message,
    send_report,
)


@pytest.fixture()
def openwa_env(monkeypatch):
    monkeypatch.setenv("OPENWA_ENABLED", "true")
    monkeypatch.setenv("OPENWA_BASE_URL", "https://owa.test")
    monkeypatch.setenv("OPENWA_API_KEY", "owa-secret-key")
    monkeypatch.setenv("OPENWA_SESSION_ID", "session-abc")
    monkeypatch.setenv("OPENWA_RECIPIENT", "96812345218")
    reset_settings_cache()
    yield get_settings()
    reset_settings_cache()


@pytest.fixture()
def report(db, controller: Controller) -> MonthlyReport:
    entry = MonthlyReport(
        controller_id=controller.id,
        month=date(2026, 7, 1),
        status=ReportStatus.FINAL,
        generated_at=datetime(2026, 8, 1, tzinfo=UTC),
        period_start=datetime(2026, 6, 30, 20, tzinfo=UTC),
        period_end=datetime(2026, 7, 31, 20, tzinfo=UTC),
        summary_json={
            "month": "2026-07",
            "metrics": {
                "pump_runtime_seconds": 54000,
                "water_estimate_liters": 168000.0,
                "energy_estimate_kwh": 60.0,
                "coverage_percent": 98.5,
            },
        },
    )
    db.add(entry)
    db.commit()
    return entry


class Recorder:
    """بوابة وهمية تسجّل الطلبات ويمكن برمجتها لتفشل."""

    def __init__(self, *, failures: int = 0, status_code: int = 200, body=None):
        self.failures = failures
        self.status_code = status_code
        self.body = body if body is not None else {"id": "msg-1", "success": True}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.failures > 0:
                self.failures -= 1
                return httpx.Response(502, text="gateway down")
            return httpx.Response(self.status_code, json=self.body)

        return httpx.MockTransport(handle)

    def client(self) -> OpenWAClient:
        return OpenWAClient(transport=self.transport())


class TestChatId:
    def test_digits_plus_suffix(self):
        assert _chat_id("+968 1234 5218", "@c.us") == "96812345218@c.us"

    def test_no_digits_is_an_error(self):
        with pytest.raises(OpenWAError):
            _chat_id("no-number", "@c.us")


class TestMessage:
    def test_carries_the_headline_numbers_and_the_link(self, report):
        text = build_message(report, "https://reports.test/r/tok")
        assert "تقرير الري الشهري" in text
        assert "يوليو 2026" in text
        assert "168.00" in text
        assert "https://reports.test/r/tok" in text
        assert "98.5" in text


class TestSend:
    def test_successful_send_records_the_delivery(self, db, report, openwa_env):
        recorder = Recorder()
        result = send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        assert result.ok
        assert result.recipient_masked == "968****5218"
        assert result.provider_message_id == "msg-1"
        assert report.status is ReportStatus.SENT
        assert len(recorder.requests) == 1

    def test_ac_010_a_second_run_does_not_send_again(self, db, report, openwa_env):
        first = Recorder()
        send_report(db, report, client=first.client(), sleep=lambda _: None)
        second = Recorder()
        result = send_report(db, report, client=second.client(), sleep=lambda _: None)
        assert result.ok
        assert second.requests == []

    def test_the_link_is_issued_and_only_its_hash_is_stored(self, db, report, openwa_env):
        recorder = Recorder()
        send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        assert report.public_token_hash and len(report.public_token_hash) == 64
        body = recorder.requests[0].content.decode()
        assert report.public_token_hash not in body

    def test_retries_then_succeeds(self, db, report, openwa_env):
        recorder = Recorder(failures=2)
        result = send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        assert result.ok
        assert len(recorder.requests) == 3

    def test_gives_up_after_the_configured_attempts(self, db, report, openwa_env):
        recorder = Recorder(failures=99)
        result = send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        assert not result.ok
        assert len(recorder.requests) == 3
        assert report.status is ReportStatus.FAILED
        delivery = db.query(NotificationDelivery).one()
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.attempt_count == 3

    def test_http_200_with_success_false_is_a_failure(self, db, report, openwa_env):
        recorder = Recorder(body={"success": False, "error": "session not connected"})
        result = send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        assert not result.ok
        assert "session not connected" in result.detail

    def test_the_stored_recipient_is_masked(self, db, report, openwa_env):
        send_report(db, report, client=Recorder().client(), sleep=lambda _: None)
        delivery = db.query(NotificationDelivery).one()
        assert delivery.recipient_masked == "968****5218"
        assert "12345218" not in delivery.recipient_masked

    def test_disabled_integration_refuses_politely(self, db, report):
        reset_settings_cache()
        result = send_report(db, report, client=Recorder().client(), sleep=lambda _: None)
        assert not result.ok
        assert "غير مفعّل" in result.detail

    def test_the_request_uses_the_configured_header_and_fields(self, db, report, openwa_env):
        recorder = Recorder()
        send_report(db, report, client=recorder.client(), sleep=lambda _: None)
        request = recorder.requests[0]
        assert request.headers["x-api-key"] == "owa-secret-key"
        assert "session-abc" in str(request.url)
        body = request.read().decode()
        assert "chatId" in body and "text" in body

    def test_force_allows_a_deliberate_resend(self, db, report, openwa_env):
        send_report(db, report, client=Recorder().client(), sleep=lambda _: None)
        again = Recorder()
        result = send_report(
            db, report, client=again.client(), sleep=lambda _: None, force=True
        )
        assert result.ok
        assert len(again.requests) == 1
