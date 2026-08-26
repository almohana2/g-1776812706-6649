"""عميل Hydrawise: القراءة فقط، الأخطاء، وحد الطلبات (SRS §5، §14، §20، AC-004)."""

from __future__ import annotations

import httpx
import pytest

from app.services.hydrawise_client import (
    ALLOWED_ENDPOINTS,
    HydrawiseAuthError,
    HydrawiseClient,
    HydrawiseError,
    HydrawiseRateLimited,
    HydrawiseUnavailable,
    InvalidHydrawisePayload,
    clamp_nextpoll,
)
from tests.mock_hydrawise import MockHydrawise, relay, status_schedule

# ``asyncio_mode = "auto"`` في pyproject يجعل الدوال غير المتزامنة هنا تعمل
# كما هي، فلا حاجة لوسم كل صنف على حدة.


def make_client(mock: MockHydrawise) -> HydrawiseClient:
    return HydrawiseClient(
        "secret-key-123", base_url="https://api.hydrawise.test/api/v1/",
        transport=mock.transport(),
    )


class TestReadOnlySurface:
    def test_only_two_endpoints_are_allowed(self):
        assert ALLOWED_ENDPOINTS == {"customerdetails.php", "statusschedule.php"}

    async def test_any_other_endpoint_is_refused_before_the_request(self):
        mock = MockHydrawise()
        client = make_client(mock)
        with pytest.raises(HydrawiseError, match="القراءة فقط"):
            await client._get("setzone.php", {"action": "run"})
        assert mock.requests == []

    def test_the_client_exposes_no_command_methods(self):
        forbidden = {"run_zone", "stop_zone", "suspend_zone", "set_zone", "setzone"}
        assert forbidden.isdisjoint(dir(HydrawiseClient))


class TestRequests:
    async def test_customer_details_parses_controllers(self):
        mock = MockHydrawise()
        details, _raw = await make_client(mock).customer_details()
        assert details.customer_id == 1337
        assert details.controllers[0].name == "ALMOHANA"
        assert mock.endpoints() == ["customerdetails.php"]

    async def test_api_key_is_sent_as_a_query_parameter(self):
        mock = MockHydrawise()
        await make_client(mock).customer_details()
        assert mock.api_keys_seen == {"secret-key-123"}

    async def test_status_schedule_passes_the_controller_id(self):
        mock = MockHydrawise(status_responses=[status_schedule([relay(1, 1, "أ")])])
        await make_client(mock).status_schedule(4242)
        assert mock.requests[-1].url.params["controller_id"] == "4242"

    async def test_a_multi_controller_account_can_omit_the_id(self):
        mock = MockHydrawise(status_responses=[status_schedule([])])
        await make_client(mock).status_schedule(None)
        assert "controller_id" not in mock.requests[-1].url.params


class TestErrors:
    async def test_http_429_raises_rate_limited_with_retry_after(self):
        mock = MockHydrawise(
            status_responses=[httpx.Response(429, headers={"Retry-After": "90"}, text="slow")]
        )
        with pytest.raises(HydrawiseRateLimited) as caught:
            await make_client(mock).status_schedule(4242)
        assert caught.value.retry_after == 90.0

    @pytest.mark.parametrize("code", [401, 403])
    async def test_rejected_key_raises_auth_error(self, code):
        mock = MockHydrawise(status_responses=[httpx.Response(code, text="nope")])
        with pytest.raises(HydrawiseAuthError):
            await make_client(mock).status_schedule(4242)

    async def test_error_msg_about_the_key_is_an_auth_error_even_on_http_200(self):
        mock = MockHydrawise(
            status_responses=[httpx.Response(200, json={"error_msg": "API key not valid"})]
        )
        with pytest.raises(HydrawiseAuthError):
            await make_client(mock).status_schedule(4242)

    async def test_error_msg_about_the_limit_is_a_rate_limit(self):
        mock = MockHydrawise(
            status_responses=[httpx.Response(200, json={"error_msg": "API rate limit exceeded"})]
        )
        with pytest.raises(HydrawiseRateLimited):
            await make_client(mock).status_schedule(4242)

    async def test_server_error_is_unavailable_not_fatal(self):
        mock = MockHydrawise(status_responses=[httpx.Response(500, text="boom")])
        with pytest.raises(HydrawiseUnavailable):
            await make_client(mock).status_schedule(4242)

    async def test_non_json_body_is_an_invalid_payload(self):
        mock = MockHydrawise(status_responses=[httpx.Response(200, text="<html>nope</html>")])
        with pytest.raises(InvalidHydrawisePayload):
            await make_client(mock).status_schedule(4242)

    async def test_timeout_becomes_unavailable(self):
        mock = MockHydrawise(status_responses=[httpx.ReadTimeout("timed out")])
        with pytest.raises(HydrawiseUnavailable):
            await make_client(mock).status_schedule(4242)

    async def test_empty_api_key_is_rejected_at_construction(self):
        with pytest.raises(HydrawiseAuthError):
            HydrawiseClient("   ")

    async def test_error_messages_never_include_the_key(self):
        mock = MockHydrawise(status_responses=[httpx.Response(500, text="boom")])
        with pytest.raises(HydrawiseUnavailable) as caught:
            await make_client(mock).status_schedule(4242)
        assert "secret-key-123" not in str(caught.value)


class TestNextPoll:
    """AC-004: لا يُرسل طلب أبكر مما تسمح به القيمة المرجعة."""

    def test_missing_nextpoll_falls_back_to_the_safe_default(self):
        assert clamp_nextpoll(None) == 60

    def test_a_too_small_value_is_raised_to_the_floor(self):
        assert clamp_nextpoll(5) == 30
        assert clamp_nextpoll(0) == 60

    def test_a_sane_value_is_honoured(self):
        assert clamp_nextpoll(120) == 120

    def test_an_absurd_value_falls_back_to_the_default(self):
        assert clamp_nextpoll(99_999) == 60

    def test_negative_values_do_not_produce_faster_polling(self):
        assert clamp_nextpoll(-10) >= 30
