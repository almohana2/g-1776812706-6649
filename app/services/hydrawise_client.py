"""عميل Hydrawise الرسمي — قراءة فقط (SRS §5، §14، §19).

هذا الملف هو نقطة التماس الوحيدة مع Hydrawise، ويقتصر عمدًا على نقطتين:

* ``customerdetails.php`` — اكتشاف الحساب والكنترولرات.
* ``statusschedule.php`` — حالة المحابس والبرنامج القادم.

``setzone.php`` **غير مُنفَّذة ولن تُنفَّذ**: التطبيق لا يشغّل مضخة ولا
يوقفها. اختبار في ``tests/unit/test_readonly_guard.py`` يحرس هذا الشرط.

قواعد التسجيل: لا يُكتب عنوان URL كاملًا لأن المفتاح في Query String؛
يُسجَّل اسم النقطة وحالة HTTP والمدة فقط.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.hydrawise import CustomerDetailsPayload, StatusSchedulePayload

logger = get_logger(__name__)

CUSTOMER_DETAILS = "customerdetails.php"
STATUS_SCHEDULE = "statusschedule.php"

#: النقاط المسموح بها. أي محاولة لاستدعاء غيرها ترفع خطأً فورًا.
ALLOWED_ENDPOINTS = frozenset({CUSTOMER_DETAILS, STATUS_SCHEDULE})


class HydrawiseError(Exception):
    """أي فشل في التخاطب مع Hydrawise."""


class HydrawiseAuthError(HydrawiseError):
    """المفتاح مرفوض أو غير صالح — يوقف الجمع (SRS §20)."""


class HydrawiseRateLimited(HydrawiseError):
    """HTTP 429 — يجب التراجع واحترام ``Retry-After``."""

    def __init__(self, retry_after: str | float | None = None) -> None:
        super().__init__("تجاوز حد الطلبات في Hydrawise")
        self.retry_after = _parse_retry_after(retry_after)


class HydrawiseUnavailable(HydrawiseError):
    """خطأ شبكة أو مهلة أو استجابة 5xx."""


class InvalidHydrawisePayload(HydrawiseError):
    """استجابة ليست JSON صالحًا أو تنقصها البنية المتوقعة."""


def _parse_retry_after(value: str | float | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RawResponse:
    """الاستجابة الخام كما وصلت — تُحفظ في ``poll_samples``."""

    status_code: int
    payload: dict[str, Any]
    elapsed_ms: int


class HydrawiseClient:
    """غلاف رفيع حول نقطتَي القراءة، بمهلة وتحقّق وتسجيل آمن."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        if not api_key or not api_key.strip():
            raise HydrawiseAuthError("لا يوجد مفتاح API لـHydrawise")
        self._api_key = api_key.strip()
        self._base_url = (base_url or settings.hydrawise_api_base).rstrip("/") + "/"
        self._timeout = timeout or settings.hydrawise_http_timeout_seconds
        self._transport = transport

    @classmethod
    def from_settings(
        cls, transport: httpx.AsyncBaseTransport | None = None
    ) -> HydrawiseClient:
        settings = get_settings()
        return cls(settings.hydrawise_api_key.get_secret_value(), transport=transport)

    # ------------------------------------------------------------------
    async def customer_details(self) -> tuple[CustomerDetailsPayload, RawResponse]:
        raw = await self._get(CUSTOMER_DETAILS, {"type": "controllers"})
        try:
            return CustomerDetailsPayload.model_validate(raw.payload), raw
        except Exception as exc:  # pydantic ValidationError
            raise InvalidHydrawisePayload(f"customerdetails غير متوقعة: {exc}") from exc

    async def status_schedule(
        self, controller_id: int | None = None
    ) -> tuple[StatusSchedulePayload, RawResponse]:
        params: dict[str, Any] = {}
        if controller_id is not None:
            params["controller_id"] = controller_id
        raw = await self._get(STATUS_SCHEDULE, params)
        try:
            return StatusSchedulePayload.model_validate(raw.payload), raw
        except Exception as exc:
            raise InvalidHydrawisePayload(f"statusschedule غير متوقعة: {exc}") from exc

    # ------------------------------------------------------------------
    async def _get(self, endpoint: str, params: dict[str, Any]) -> RawResponse:
        if endpoint not in ALLOWED_ENDPOINTS:
            # حارس صريح: النظام للقراءة فقط (SRS §5.4، §19).
            raise HydrawiseError(f"النقطة {endpoint} غير مسموح بها في نظام القراءة فقط")

        query = {"api_key": self._api_key, **{k: v for k, v in params.items() if v is not None}}
        started = time.monotonic()
        timeout = httpx.Timeout(self._timeout, connect=min(10.0, self._timeout))
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.get(self._base_url + endpoint, params=query)
        except httpx.TimeoutException as exc:
            raise HydrawiseUnavailable(f"انتهت مهلة الطلب إلى {endpoint}") from exc
        except httpx.HTTPError as exc:
            raise HydrawiseUnavailable(f"تعذّر الوصول إلى {endpoint}: {type(exc).__name__}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "hydrawise.request",
            extra={
                "endpoint": endpoint,           # لا يُسجَّل العنوان الكامل
                "http_status": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )

        if response.status_code == 429:
            raise HydrawiseRateLimited(response.headers.get("Retry-After"))
        if response.status_code in (401, 403):
            raise HydrawiseAuthError("Hydrawise رفض مفتاح الـAPI")
        if response.status_code >= 500:
            raise HydrawiseUnavailable(f"Hydrawise أعاد HTTP {response.status_code}")
        if response.status_code >= 400:
            raise HydrawiseError(f"Hydrawise أعاد HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise InvalidHydrawisePayload("الاستجابة ليست JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidHydrawisePayload("الاستجابة ليست كائن JSON")

        # Hydrawise يعيد كثيرًا من الأخطاء بحالة 200 مع حقل error_msg.
        error = payload.get("error_msg") or payload.get("error")
        if error:
            message = str(error)
            lowered = message.lower()
            if "api key" in lowered or "apikey" in lowered or "not valid" in lowered:
                raise HydrawiseAuthError(message)
            if "rate" in lowered or "too many" in lowered or "limit" in lowered:
                raise HydrawiseRateLimited(None)
            raise HydrawiseError(message)

        return RawResponse(
            status_code=response.status_code, payload=payload, elapsed_ms=elapsed_ms
        )


def clamp_nextpoll(value: int | None) -> int:
    """يحوّل ``nextpoll`` إلى فاصل آمن (SRS §5.5، §24.5).

    القيمة المفقودة أو الشاذة تُستبدل بالافتراضي، ولا يُسمح أبدًا بفاصل
    أقصر من الحد الأدنى المضبوط — أي أن الخطأ يميل دائمًا إلى الإبطاء.
    """
    settings = get_settings()
    if value is None or value <= 0 or value > settings.hydrawise_max_poll_seconds:
        return settings.hydrawise_default_poll_seconds
    return max(settings.hydrawise_min_poll_seconds, value)
