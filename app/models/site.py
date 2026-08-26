"""الكنترولرات والمحابس وبيانات المضخة (SRS §8.1–§8.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.models.enums import CalibrationMethod


class Controller(Base, TimestampMixin):
    """كنترولر Hydrawise واحد كما اكتُشف من الـAPI."""

    __tablename__ = "controllers"

    id: Mapped[uuid.UUID] = uuid_pk()
    hydrawise_controller_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Muscat"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_successful_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    zones: Mapped[list[Zone]] = relationship(
        back_populates="controller", cascade="all, delete-orphan"
    )
    pump_profile: Mapped[PumpProfile | None] = relationship(
        back_populates="controller",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def masked_serial(self) -> str:
        """لا يُعرض الرقم التسلسلي كاملًا في الواجهة (SRS §8.1)."""
        serial = self.serial_number or ""
        if len(serial) <= 4:
            return "*" * len(serial)
        return "*" * (len(serial) - 4) + serial[-4:]


class Zone(Base, TimestampMixin):
    """محبس ري واحد — الـAPI يسميه relay."""

    __tablename__ = "zones"
    __table_args__ = (
        UniqueConstraint(
            "controller_id", "hydrawise_relay_id", name="zones_controller_relay"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    controller_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("controllers.id", ondelete="CASCADE"), nullable=False
    )
    hydrawise_relay_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    physical_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name_ar: Mapped[str | None] = mapped_column(String(160), nullable=True)
    flow_rate_lpm: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("140.00")
    )
    flow_min_lpm: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("80.00")
    )
    flow_max_lpm: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("200.00")
    )
    calibration_method: Mapped[CalibrationMethod] = mapped_column(
        Enum(CalibrationMethod, name="calibration_method", native_enum=True),
        nullable=False,
        default=CalibrationMethod.DEFAULT,
    )
    calibrated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    missing_sync_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    controller: Mapped[Controller] = relationship(back_populates="zones")

    @property
    def label(self) -> str:
        """الاسم المعروض — العربي إن وُجد، وإلا اسم Hydrawise."""
        return self.display_name_ar or self.name

    @property
    def is_calibrated(self) -> bool:
        return self.calibration_method is not CalibrationMethod.DEFAULT


class PumpProfile(Base, TimestampMixin):
    """لوحة بيانات المضخة والمحرك، ومنها تُقدَّر الطاقة (SRS §4.2–§4.4)."""

    __tablename__ = "pump_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()
    controller_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("controllers.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    brand: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rated_hp: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    rated_kw: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    estimated_input_kw: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=Decimal("4.000")
    )
    voltage_min: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    voltage_max: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    rated_current_a: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    frequency_hz: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_min_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    head_max_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    flow_min_lpm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    flow_default_lpm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    flow_max_lpm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    well_depth_m: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    controller: Mapped[Controller] = relationship(back_populates="pump_profile")
