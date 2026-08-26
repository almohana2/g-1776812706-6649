"""التقارير الشهرية وسجل الإرسال (SRS §8.7–§8.8)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import DeliveryChannel, DeliveryStatus, ReportStatus
from app.models.site import Controller


class MonthlyReport(Base, TimestampMixin):
    """نسخة مجمّدة من أرقام شهر واحد.

    ``summary_json`` هو مصدر الحقيقة للعرض: يُجمَّد وقت التوليد حتى لا تتغير
    أرقام تقرير صدر بالفعل عند تعديل معدل التدفق لاحقًا (SRS §FR-007، §FR-012).
    """

    __tablename__ = "monthly_reports"
    __table_args__ = (
        UniqueConstraint("controller_id", "month", name="monthly_reports_controller_month"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    controller_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("controllers.id", ondelete="CASCADE"), nullable=False
    )
    month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status", native_enum=True),
        nullable=False,
        default=ReportStatus.DRAFT,
    )
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_zone_runtime_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    pump_union_runtime_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    total_water_liters_estimate: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )
    total_water_liters_min: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )
    total_water_liters_max: Mapped[Decimal] = mapped_column(
        Numeric(16, 2), nullable=False, default=Decimal("0")
    )
    energy_kwh_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 3), nullable=False, default=Decimal("0")
    )
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_coverage_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    summary_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    html_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    public_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    controller: Mapped[Controller] = relationship(lazy="joined")
    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )

    @property
    def month_key(self) -> str:
        return f"{self.month.year:04d}-{self.month.month:02d}"


class NotificationDelivery(Base, TimestampMixin):
    """محاولة إرسال واحدة لتقرير عبر قناة واحدة."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        # مفتاح الحماية من التكرار: تقرير + قناة + مستلم (SRS §9.8).
        UniqueConstraint(
            "report_id", "channel", "recipient_masked", name="notification_idempotency"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("monthly_reports.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[DeliveryChannel] = mapped_column(
        Enum(DeliveryChannel, name="delivery_channel", native_enum=True), nullable=False
    )
    recipient_masked: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status", native_enum=True),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    report: Mapped[MonthlyReport] = relationship(back_populates="deliveries")
