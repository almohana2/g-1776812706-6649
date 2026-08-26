"""قوائم القيم الثابتة المستخدمة في الجداول (SRS §8).

ملاحظة تخزين: SQLAlchemy يحفظ **اسم** العنصر لا قيمته، أي ``HIGH`` في
قاعدة البيانات و``high`` في الواجهة و JSON. الاثنان متوافقان ما دامت
الأسماء لا تتغير؛ تغيير اسم عنصر يحتاج ترحيلة.
"""

from __future__ import annotations

import enum


class CalibrationMethod(enum.StrEnum):
    """كيف عُرف معدل تدفق المحبس."""

    DEFAULT = "default"
    MANUAL = "manual"
    FLOW_METER = "flow_meter"
    PUMP_CURVE = "pump_curve"


class EventSource(enum.StrEnum):
    """من أين جاء حدث التشغيل."""

    API_OBSERVED = "api_observed"
    API_INFERRED = "api_inferred"
    MANUAL_IMPORT = "manual_import"
    MANUAL_ADJUSTMENT = "manual_adjustment"


class Confidence(enum.StrEnum):
    """مستوى الثقة في زمن الحدث."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    def downgrade(self) -> Confidence:
        """خفض درجة واحدة عند فقد معلومة (SRS §9.6)."""
        if self is Confidence.HIGH:
            return Confidence.MEDIUM
        return Confidence.LOW


class GapReason(enum.StrEnum):
    """سبب انقطاع الجمع."""

    NETWORK = "network"
    API_429 = "api_429"
    API_ERROR = "api_error"
    INVALID_PAYLOAD = "invalid_payload"
    WORKER_DOWN = "worker_down"


class ReportStatus(enum.StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    SENT = "sent"
    FAILED = "failed"


class DeliveryChannel(enum.StrEnum):
    OPENWA = "openwa"
    EMAIL = "email"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class UserRole(enum.StrEnum):
    """صلاحيتان على الأقل حسب SRS §NFR-003."""

    ADMIN = "admin"
    VIEWER = "viewer"
