"""العينات الخام وأحداث التشغيل وفجوات البيانات (SRS §8.4–§8.6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import Confidence, EventSource, GapReason
from app.models.site import Zone


class PollSample(Base):
    """استجابة ``statusschedule`` واحدة، محفوظة للتدقيق وإعادة البناء.

    الحمولة تُخزَّن بعد إزالة أي أثر للمفتاح؛ الطلب يُرسل بالمفتاح في
    Query String لكن الاستجابة لا تحتويه، والحفظ هنا للاستجابة فقط.
    """

    __tablename__ = "poll_samples"
    __table_args__ = (
        Index("ix_poll_samples_controller_observed", "controller_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    controller_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("controllers.id", ondelete="CASCADE"), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source_epoch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    nextpoll_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ZoneRuntimeEvent(Base, TimestampMixin):
    """دورة تشغيل واحدة لمحبس واحد، من الفتح إلى الإغلاق."""

    __tablename__ = "zone_runtime_events"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="ended_after_started"
        ),
        CheckConstraint(
            "runtime_seconds IS NULL OR runtime_seconds >= 0", name="runtime_non_negative"
        ),
        # حدث مفتوح واحد فقط لكل محبس (SRS §8.5) — يفرضه فهرس جزئي فريد.
        Index(
            "uq_zone_runtime_events_open_zone",
            "zone_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_zone_runtime_events_zone_started", "zone_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    zone_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("zones.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_sample_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("poll_samples.id", ondelete="SET NULL"), nullable=True
    )
    end_sample_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("poll_samples.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[EventSource] = mapped_column(
        Enum(EventSource, name="event_source", native_enum=True),
        nullable=False,
        default=EventSource.API_OBSERVED,
    )
    confidence: Mapped[Confidence] = mapped_column(
        Enum(Confidence, name="event_confidence", native_enum=True),
        nullable=False,
        default=Confidence.MEDIUM,
    )
    planned_runtime_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_remaining_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_running_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    water_liters_estimate: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    flow_rate_lpm_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    flow_min_lpm_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    flow_max_lpm_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    is_adjusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    zone: Mapped[Zone] = relationship(lazy="joined")

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class DataGap(Base):
    """فترة لم يصل فيها جمع ناجح — تُستخدم في حساب التغطية."""

    __tablename__ = "data_gaps"
    __table_args__ = (
        Index("ix_data_gaps_controller_started", "controller_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    controller_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("controllers.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[GapReason] = mapped_column(
        Enum(GapReason, name="gap_reason", native_enum=True), nullable=False
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    may_affect_runtime: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None
