"""الحالة الحيّة للوحة وصفحة الصحة (SRS §FR-005، §NFR-005).

المصدر هو آخر عينة ناجحة محفوظة، لا طلب جديد إلى Hydrawise: فتح الصفحة
يجب ألا يستهلك من حد الطلبات ولا يخالف ``nextpoll``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import Controller, DataGap, PollSample, Zone, ZoneRuntimeEvent
from app.schemas.hydrawise import StatusSchedulePayload
from app.services.event_engine import gap_threshold_seconds, observations_from_payload

__all__ = ["ZoneStatus", "RunningZone", "ControllerStatus", "controller_status"]


@dataclass
class ZoneStatus:
    number: int | None
    name: str
    is_running: bool
    is_scheduled: bool
    next_run_text: str | None
    last_water_text: str | None


@dataclass
class RunningZone:
    zone_name: str
    elapsed_seconds: float
    remaining_seconds: int | None


@dataclass
class ControllerStatus:
    controller: Controller
    online: bool
    seconds_since_poll: float | None
    gap_open: bool
    zones: list[ZoneStatus] = field(default_factory=list)
    running: list[RunningZone] = field(default_factory=list)
    samples_last_24h: int = 0
    open_events: int = 0

    @property
    def state_label(self) -> tuple[str, str]:
        if self.seconds_since_poll is None:
            return "لم يبدأ الجمع", "muted"
        if self.online:
            return "يعمل", "good"
        return "متأخر", "bad"


def _latest_sample(db: Session, controller: Controller) -> PollSample | None:
    return db.execute(
        select(PollSample)
        .where(PollSample.controller_id == controller.id)
        .where(PollSample.is_success.is_(True))
        .order_by(PollSample.observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def controller_status(
    db: Session, controller: Controller, *, now: datetime | None = None
) -> ControllerStatus:
    """يبني صورة الحالة الحالية لكنترولر واحد."""
    moment = now or utcnow()
    sample = _latest_sample(db, controller)
    seconds_since = (
        (moment - sample.observed_at).total_seconds() if sample is not None else None
    )
    threshold = gap_threshold_seconds(
        sample.nextpoll_seconds if sample is not None else None
    )
    online = seconds_since is not None and seconds_since <= threshold

    gap_open = db.execute(
        select(DataGap.id)
        .where(DataGap.controller_id == controller.id)
        .where(DataGap.ended_at.is_(None))
        .limit(1)
    ).first() is not None

    zones: list[ZoneStatus] = []
    if sample is not None and sample.payload:
        try:
            payload = StatusSchedulePayload.model_validate(sample.payload)
        except Exception:
            payload = None
        if payload is not None:
            for observation in observations_from_payload(payload):
                zones.append(
                    ZoneStatus(
                        number=observation.physical_number,
                        name=observation.name,
                        is_running=observation.is_running,
                        is_scheduled=(
                            observation.seconds_until_next_run is not None
                            and not observation.is_running
                            and observation.seconds_until_next_run < 1_576_800_000
                        ),
                        next_run_text=observation.next_run_text,
                        last_water_text=observation.last_water_text,
                    )
                )

    running_rows = db.execute(
        select(ZoneRuntimeEvent, Zone)
        .join(Zone, Zone.id == ZoneRuntimeEvent.zone_id)
        .where(Zone.controller_id == controller.id)
        .where(ZoneRuntimeEvent.ended_at.is_(None))
        .order_by(ZoneRuntimeEvent.started_at)
    ).all()
    running = [
        RunningZone(
            zone_name=zone.label,
            elapsed_seconds=max(0.0, (moment - event.started_at).total_seconds()),
            remaining_seconds=event.last_remaining_seconds,
        )
        for event, zone in running_rows
    ]

    samples_24h = int(
        db.execute(
            select(func.count(PollSample.id))
            .where(PollSample.controller_id == controller.id)
            .where(PollSample.observed_at >= moment - timedelta(hours=24))
            .where(PollSample.is_success.is_(True))
        ).scalar_one()
    )

    return ControllerStatus(
        controller=controller,
        online=online,
        seconds_since_poll=seconds_since,
        gap_open=gap_open,
        zones=zones,
        running=running,
        samples_last_24h=samples_24h,
        open_events=len(running_rows),
    )
