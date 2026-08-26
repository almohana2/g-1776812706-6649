"""نماذج الإدخال والإخراج لواجهة التطبيق الداخلية (SRS §13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.models.enums import CalibrationMethod, Confidence, EventSource


class HealthOut(BaseModel):
    status: str
    database: bool
    hydrawise_configured: bool
    openwa_configured: bool
    version: str = "1.0.0"


class ControllerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hydrawise_controller_id: int
    name: str
    timezone: str
    is_active: bool
    last_successful_poll_at: datetime | None = None
    serial_masked: str = Field(default="", alias="masked_serial")


class ZoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hydrawise_relay_id: int
    physical_number: int | None
    name: str
    display_name_ar: str | None
    flow_rate_lpm: Decimal
    flow_min_lpm: Decimal
    flow_max_lpm: Decimal
    calibration_method: CalibrationMethod
    calibrated_at: datetime | None
    is_active: bool


class ZoneUpdateIn(BaseModel):
    """تعديل محبس — التدفق والاسم فقط؛ لا شيء هنا يشغّل محبسًا."""

    display_name_ar: str | None = None
    flow_rate_lpm: float | None = Field(default=None, gt=0, le=10_000)
    flow_min_lpm: float | None = Field(default=None, gt=0, le=10_000)
    flow_max_lpm: float | None = Field(default=None, gt=0, le=10_000)
    calibration_method: CalibrationMethod | None = None
    reason: str | None = Field(default=None, max_length=500)

    def flow_bounds_ok(self, current_min: float, current_mid: float, current_max: float) -> bool:
        low = self.flow_min_lpm if self.flow_min_lpm is not None else current_min
        mid = self.flow_rate_lpm if self.flow_rate_lpm is not None else current_mid
        high = self.flow_max_lpm if self.flow_max_lpm is not None else current_max
        return low <= mid <= high


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    zone_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    runtime_seconds: int | None
    confidence: Confidence
    source: EventSource
    water_liters_estimate: Decimal | None
    flow_rate_lpm_snapshot: Decimal | None
    is_adjusted: bool
    adjustment_reason: str | None


class EventCreateIn(BaseModel):
    """إضافة حدث يدويًا — للإدارة فقط ومع سبب إلزامي (SRS §FR-013)."""

    zone_id: uuid.UUID
    started_at: datetime
    ended_at: datetime
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("ended_at")
    @classmethod
    def _after_start(cls, value: datetime, info: ValidationInfo) -> datetime:
        start = info.data.get("started_at")
        if start is not None and value <= start:
            raise ValueError("نهاية الحدث يجب أن تكون بعد بدايته")
        return value


class EventUpdateIn(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exclude: bool = False
    reason: str = Field(min_length=3, max_length=500)


class CollectorStatusOut(BaseModel):
    controller: str
    last_successful_poll_at: datetime | None
    seconds_since_poll: float | None
    healthy: bool
    open_gap: bool
    samples_last_24h: int
    open_events: int


class SendResultOut(BaseModel):
    status: str
    detail: str = ""
    recipient_masked: str = ""
    provider_message_id: str | None = None
