"""إرسال رابط التقرير عبر بوابة OpenWA (SRS §15، §FR-011).

ترويسة المصادقة ومسار الإرسال وأسماء حقول الجسم كلها من الإعدادات، لأن
صيغ بوابات OpenWA تختلف بين الإصدارات؛ افتراض اسم ترويسة ثابت في الكود
يعني كسرًا صامتًا عند أول ترقية للبوابة.

الحماية من التكرار: سجل تسليم واحد لكل (تقرير، قناة، مستلم). إن كان
موجودًا بحالة ``sent`` لا تُرسل رسالة ثانية مهما أُعيد تشغيل المهمة.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger, mask_phone, redact
from app.core.time import month_key, utcnow
from app.models import (
    Controller,
    DeliveryChannel,
    DeliveryStatus,
    MonthlyReport,
    NotificationDelivery,
    ReportStatus,
)
from app.services import audit
from app.services.exports import month_label_ar
from app.services.sharing import issue_share_token, public_url

logger = get_logger(__name__)

RETRY_BACKOFF_SECONDS = (5, 20, 60)

MESSAGE_TEMPLATE = """تقرير الري الشهري — {month_ar}

إجمالي تشغيل المضخة: {pump_hours}
استهلاك المياه التقديري: {water_m3} م³
الطاقة التقديرية: {energy_kwh} kWh
جودة البيانات: {coverage}%

للاطلاع على التقرير الكامل:
{report_url}"""


class OpenWAError(RuntimeError):
    """فشل الإرسال عبر البوابة."""


@dataclass
class SendResult:
    status: DeliveryStatus
    detail: str = ""
    recipient_masked: str = ""
    provider_message_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is DeliveryStatus.SENT


def _chat_id(recipient: str, suffix: str) -> str:
    """يبني معرّف المحادثة كما تتوقعه البوابة (رقم + لاحقة)."""
    number = "".join(ch for ch in recipient if ch.isdigit())
    if not number:
        raise OpenWAError("رقم المستلم غير صالح")
    return number + (suffix or "")


def _extract_message_id(payload: Any) -> str | None:
    """يلتقط معرّف الرسالة من صيغ استجابة مختلفة بين إصدارات البوابة."""
    if not isinstance(payload, dict):
        return None
    for key in ("id", "messageId", "message_id", "idMessage"):
        value = payload.get(key)
        if isinstance(value, str | int):
            return str(value)
    for container in ("response", "data", "result", "key"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            found = _extract_message_id(nested)
            if found:
                return found
        if isinstance(nested, str | int):
            return str(nested)
    return None


class OpenWAClient:
    """عميل HTTP رفيع للبوابة، قابل للحقن في الاختبارات."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = get_settings()
        self._transport = transport

    def send_text(self, recipient: str, text: str) -> tuple[str | None, str]:
        """يرسل رسالة نصية ويعيد ``(معرّف الرسالة، ملخص الاستجابة)``."""
        settings = self.settings
        session_id = settings.openwa_session_id.get_secret_value()
        if not settings.openwa_base_url or not session_id:
            raise OpenWAError("إعدادات OpenWA ناقصة")

        path = settings.openwa_send_path.format(session_id=session_id)
        url = settings.openwa_base_url.rstrip("/") + "/" + path.lstrip("/")
        api_key = settings.openwa_api_key.get_secret_value()
        headers = {"Content-Type": "application/json"}
        if api_key:
            value = f"{settings.openwa_auth_scheme} {api_key}".strip()
            headers[settings.openwa_auth_header] = value
        body = {
            settings.openwa_recipient_field: _chat_id(
                recipient, settings.openwa_recipient_suffix
            ),
            settings.openwa_text_field: text,
        }

        try:
            with httpx.Client(
                timeout=settings.openwa_timeout_seconds, transport=self._transport
            ) as client:
                response = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OpenWAError(f"تعذّر الوصول إلى البوابة: {type(exc).__name__}") from exc

        # لا يُسجَّل الجسم: قد يحتوي الرقم أو الرمز.
        logger.info(
            "openwa.request",
            extra={"http_status": response.status_code, "endpoint": "send-text"},
        )
        if response.status_code >= 400:
            raise OpenWAError(f"البوابة أعادت HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        # بعض الإصدارات تعيد 200 مع success=false — النجاح ليس حالة HTTP وحدها.
        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error") or payload.get("message") or "رفضت البوابة الرسالة"
            raise OpenWAError(redact(str(error)))
        return _extract_message_id(payload), f"HTTP {response.status_code}"


def build_message(report: MonthlyReport, report_url: str) -> str:
    payload = report.summary_json or {}
    metrics = payload.get("metrics", {})
    pump_hours = metrics.get("pump_runtime_seconds", 0) / 3600.0
    return MESSAGE_TEMPLATE.format(
        month_ar=month_label_ar(report.month_key),
        pump_hours=f"{pump_hours:,.1f} ساعة",
        water_m3=f"{metrics.get('water_estimate_liters', 0) / 1000.0:,.2f}",
        energy_kwh=f"{metrics.get('energy_estimate_kwh', 0):,.1f}",
        coverage=f"{metrics.get('coverage_percent', 0):,.1f}",
        report_url=report_url,
    )


def send_report(
    db: Session,
    report: MonthlyReport,
    *,
    client: OpenWAClient | None = None,
    actor: str = "worker",
    sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
) -> SendResult:
    """يرسل رابط التقرير مرة واحدة، مع ثلاث محاولات عند الفشل."""
    settings = get_settings()
    if not settings.openwa_configured:
        return SendResult(DeliveryStatus.FAILED, "OpenWA غير مفعّل")

    recipient = settings.openwa_recipient
    masked = mask_phone(recipient)
    delivery = db.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.report_id == report.id)
        .where(NotificationDelivery.channel == DeliveryChannel.OPENWA)
        .where(NotificationDelivery.recipient_masked == masked)
    ).scalar_one_or_none()

    if delivery is not None and delivery.status is DeliveryStatus.SENT and not force:
        # نجاح موثّق سابقًا: إعادة تشغيل المهمة لا ترسل نسخة ثانية (AC-010).
        return SendResult(
            DeliveryStatus.SENT,
            "أُرسل سابقًا",
            masked,
            delivery.provider_message_id,
        )
    if delivery is None:
        delivery = NotificationDelivery(
            report_id=report.id,
            channel=DeliveryChannel.OPENWA,
            recipient_masked=masked,
            status=DeliveryStatus.PENDING,
        )
        db.add(delivery)
        db.flush()

    token = issue_share_token(report)
    url = public_url(token)
    message = build_message(report, url)
    sender = client or OpenWAClient()

    last_error = ""
    for attempt in range(settings.openwa_max_attempts):
        delivery.attempt_count += 1
        try:
            message_id, detail = sender.send_text(recipient, message)
        except OpenWAError as exc:
            last_error = redact(str(exc))
            logger.warning(
                "openwa.attempt_failed",
                extra={"attempt": attempt + 1, "error": last_error, "recipient": masked},
            )
            if attempt + 1 < settings.openwa_max_attempts:
                sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
            continue

        delivery.status = DeliveryStatus.SENT
        delivery.provider_message_id = message_id
        delivery.sent_at = utcnow()
        delivery.last_error = None
        report.status = ReportStatus.SENT
        audit.record(
            db, actor=actor, action="report.sent", entity_type="monthly_report",
            entity_id=str(report.id), after={"recipient": masked, "message_id": message_id},
        )
        logger.info("openwa.sent", extra={"recipient": masked, "month": report.month_key})
        return SendResult(DeliveryStatus.SENT, detail, masked, message_id)

    delivery.status = DeliveryStatus.FAILED
    delivery.last_error = last_error[:2000]
    report.status = ReportStatus.FAILED
    audit.record(
        db, actor=actor, action="report.send_failed", entity_type="monthly_report",
        entity_id=str(report.id), reason=last_error,
    )
    return SendResult(DeliveryStatus.FAILED, last_error, masked)


def send_monthly_report(
    db: Session, controller: Controller, *, year: int, month: int, actor: str = "worker"
) -> SendResult:
    """يرسل تقرير شهر محدد لكنترولر محدد، إن كان مولّدًا."""
    from datetime import date

    report = db.execute(
        select(MonthlyReport)
        .where(MonthlyReport.controller_id == controller.id)
        .where(MonthlyReport.month == date(year, month, 1))
    ).scalar_one_or_none()
    if report is None:
        return SendResult(DeliveryStatus.FAILED, f"لا يوجد تقرير لشهر {month_key(year, month)}")
    return send_report(db, report, actor=actor)
