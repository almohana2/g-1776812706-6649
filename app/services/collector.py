"""جامع البيانات: اكتشاف، استطلاع، أحداث، فجوات (SRS §9، §FR-002..FR-004).

كل استطلاع ناجح يمر بنفس الخطوات:

1. حفظ العينة الخام في ``poll_samples`` للتدقيق وإعادة البناء.
2. مطابقة المحابس (إنشاء الجديد، تعطيل الغائب بعد تأكيد).
3. تمرير ملاحظات المحابس على آلة الحالات وتطبيق قراراتها.
4. إغلاق أي فجوة بيانات مفتوحة، وتحديث آخر اتصال ناجح.

الفشل لا يكسر الـWorker: يُحفظ كعينة فاشلة، وتُفتح فجوة، ويُحسب فاصل
تراجع تصاعدي، ثم تُعاد المحاولة.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models import (
    Confidence,
    Controller,
    DataGap,
    EventSource,
    GapReason,
    PollSample,
    Zone,
    ZoneRuntimeEvent,
)
from app.schemas.hydrawise import StatusSchedulePayload
from app.services import audit
from app.services.event_engine import (
    CloseRun,
    OpenRunState,
    ZoneObservation,
    gap_threshold_seconds,
    observations_from_payload,
    plan_close,
    plan_extend,
    plan_stale_close,
    plan_start,
)
from app.services.hydrawise_client import (
    HydrawiseAuthError,
    HydrawiseClient,
    HydrawiseError,
    HydrawiseRateLimited,
    HydrawiseUnavailable,
    InvalidHydrawisePayload,
    clamp_nextpoll,
)

logger = get_logger(__name__)

#: تراجع تصاعدي عند فشل الشبكة (SRS §5.5).
NETWORK_BACKOFF_SECONDS = (30, 60, 120, 300)


@dataclass
class PollOutcome:
    """نتيجة استطلاع واحد لكنترولر واحد."""

    controller_id: uuid.UUID
    ok: bool
    next_poll_seconds: int
    observed_at: datetime
    started_events: int = 0
    extended_events: int = 0
    closed_events: int = 0
    new_zones: int = 0
    error_code: str | None = None
    error_message: str | None = None
    fatal: bool = False  # مفتاح غير صالح: يوقف الجمع بدل تكرار الفشل
    notes: list[str] = field(default_factory=list)


def _payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal(value: float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class Collector:
    """يجمع لكنترولر واحد أو أكثر باستخدام عميل Hydrawise للقراءة فقط."""

    def __init__(self, client: HydrawiseClient) -> None:
        self.client = client
        self.settings = get_settings()
        self._failure_streak: dict[uuid.UUID, int] = {}

    # ------------------------------------------------------------------
    # الاكتشاف والمزامنة
    # ------------------------------------------------------------------
    async def sync_controllers(self, db: Session) -> list[Controller]:
        """يكتشف الكنترولرات من الـAPI ويحدّثها محليًا (SRS §9.1، §FR-002)."""
        details, _raw = await self.client.customer_details()
        seen: list[Controller] = []

        entries = list(details.controllers)
        if not entries and details.controller_id is not None:
            # حساب بكنترولر واحد قد لا يرسل المصفوفة.
            entries = []
            controller = self._upsert_controller(
                db,
                controller_id=details.controller_id,
                name=details.current_controller or "Controller",
                serial=None,
                customer_id=details.customer_id,
            )
            seen.append(controller)

        for entry in entries:
            if entry.controller_id is None:
                continue
            seen.append(
                self._upsert_controller(
                    db,
                    controller_id=entry.controller_id,
                    name=entry.name or f"Controller {entry.controller_id}",
                    serial=entry.serial_number,
                    customer_id=details.customer_id,
                )
            )

        db.flush()
        logger.info("collector.controllers_synced", extra={"count": len(seen)})
        return seen

    def _upsert_controller(
        self,
        db: Session,
        *,
        controller_id: int,
        name: str,
        serial: str | None,
        customer_id: int | None,
    ) -> Controller:
        controller = db.execute(
            select(Controller).where(Controller.hydrawise_controller_id == controller_id)
        ).scalar_one_or_none()
        if controller is None:
            controller = Controller(
                hydrawise_controller_id=controller_id,
                name=name[:160],
                serial_number=serial,
                customer_id=customer_id,
                timezone=self.settings.report_timezone,
            )
            db.add(controller)
            db.flush()
            audit.record(
                db, actor="collector", action="controller.discovered",
                entity_type="controller", entity_id=str(controller.id),
                after={"hydrawise_controller_id": controller_id, "name": name},
            )
        else:
            controller.name = name[:160]
            if serial:
                controller.serial_number = serial
            if customer_id is not None:
                controller.customer_id = customer_id
            controller.is_active = True
        return controller

    def sync_zones(
        self, db: Session, controller: Controller, observations: list[ZoneObservation]
    ) -> int:
        """يطابق المحابس المرصودة مع قاعدة البيانات ويعيد عدد الجديد.

        المحبس الغائب لا يُحذف أبدًا — تاريخه يبقى — بل يُعطَّل بعد غيابه
        عن أكثر من مزامنة واحدة (SRS §FR-002، §20).
        """
        existing = {
            zone.hydrawise_relay_id: zone
            for zone in db.execute(
                select(Zone).where(Zone.controller_id == controller.id)
            ).scalars()
        }
        created = 0
        seen_ids: set[int] = set()

        for observation in observations:
            seen_ids.add(observation.relay_id)
            zone = existing.get(observation.relay_id)
            if zone is None:
                zone = Zone(
                    controller_id=controller.id,
                    hydrawise_relay_id=observation.relay_id,
                    physical_number=observation.physical_number,
                    name=observation.name[:160],
                    flow_rate_lpm=_decimal(self.settings.default_flow_lpm),
                    flow_min_lpm=_decimal(self.settings.default_flow_min_lpm),
                    flow_max_lpm=_decimal(self.settings.default_flow_max_lpm),
                )
                db.add(zone)
                db.flush()
                existing[observation.relay_id] = zone
                created += 1
                audit.record(
                    db, actor="collector", action="zone.discovered",
                    entity_type="zone", entity_id=str(zone.id),
                    after={"relay_id": observation.relay_id, "name": observation.name},
                )
            else:
                zone.name = observation.name[:160] or zone.name
                if observation.physical_number is not None:
                    zone.physical_number = observation.physical_number
                zone.missing_sync_count = 0
                zone.is_active = True

        for relay_id, zone in existing.items():
            if relay_id in seen_ids or not zone.is_active:
                continue
            zone.missing_sync_count += 1
            if zone.missing_sync_count >= 2:
                zone.is_active = False
                audit.record(
                    db, actor="collector", action="zone.deactivated",
                    entity_type="zone", entity_id=str(zone.id),
                    reason="غاب المحبس عن أكثر من مزامنة",
                )
        return created

    # ------------------------------------------------------------------
    # الاستطلاع
    # ------------------------------------------------------------------
    async def poll(self, db: Session, controller: Controller) -> PollOutcome:
        """استطلاع واحد كامل لكنترولر واحد."""
        observed_at = utcnow()
        try:
            payload, raw = await self.client.status_schedule(
                controller.hydrawise_controller_id
            )
        except HydrawiseAuthError as exc:
            return self._handle_failure(
                db, controller, observed_at, GapReason.API_ERROR,
                "auth", str(exc), fatal=True, next_seconds=NETWORK_BACKOFF_SECONDS[-1],
            )
        except HydrawiseRateLimited as exc:
            wait = int(exc.retry_after or 0) or self.settings.hydrawise_max_poll_seconds
            return self._handle_failure(
                db, controller, observed_at, GapReason.API_429,
                "rate_limited", str(exc), next_seconds=wait,
            )
        except InvalidHydrawisePayload as exc:
            return self._handle_failure(
                db, controller, observed_at, GapReason.INVALID_PAYLOAD,
                "invalid_payload", str(exc),
            )
        except HydrawiseUnavailable as exc:
            return self._handle_failure(
                db, controller, observed_at, GapReason.NETWORK, "network", str(exc)
            )
        except HydrawiseError as exc:
            return self._handle_failure(
                db, controller, observed_at, GapReason.API_ERROR, "api_error", str(exc)
            )

        self._failure_streak.pop(controller.id, None)
        next_poll = clamp_nextpoll(payload.nextpoll)
        sample = PollSample(
            controller_id=controller.id,
            observed_at=observed_at,
            source_epoch=payload.time,
            nextpoll_seconds=next_poll,
            http_status=raw.status_code,
            payload_hash=_payload_hash(raw.payload),
            payload=raw.payload,
            is_success=True,
        )
        db.add(sample)
        db.flush()

        observations = observations_from_payload(payload)
        outcome = PollOutcome(
            controller_id=controller.id,
            ok=True,
            next_poll_seconds=next_poll,
            observed_at=observed_at,
        )
        outcome.new_zones = self.sync_zones(db, controller, observations)

        previous_sample = self._previous_successful_sample(db, controller, sample.id)
        previous_planned = self._planned_from_sample(previous_sample)
        previous_nextpoll = (
            previous_sample.nextpoll_seconds if previous_sample is not None else next_poll
        )

        (
            outcome.started_events,
            outcome.extended_events,
            outcome.closed_events,
        ) = self._apply_observations(
            db,
            controller,
            observations,
            observed_at=observed_at,
            sample_id=sample.id,
            previous_planned=previous_planned,
            previous_nextpoll=previous_nextpoll or next_poll,
        )
        self._close_open_gap(db, controller, observed_at)
        controller.last_successful_poll_at = observed_at
        return outcome

    # ------------------------------------------------------------------
    def _apply_observations(
        self,
        db: Session,
        controller: Controller,
        observations: list[ZoneObservation],
        *,
        observed_at: datetime,
        sample_id: int,
        previous_planned: dict[int, int],
        previous_nextpoll: int,
    ) -> tuple[int, int, int]:
        """يطبّق قرارات آلة الحالات ويعيد ``(فُتح، مُدّد، أُغلق)``."""
        zones = {
            zone.hydrawise_relay_id: zone
            for zone in db.execute(
                select(Zone).where(Zone.controller_id == controller.id)
            ).scalars()
        }
        open_events = {
            event.zone_id: event
            for event in db.execute(
                select(ZoneRuntimeEvent)
                .join(Zone, Zone.id == ZoneRuntimeEvent.zone_id)
                .where(Zone.controller_id == controller.id)
                .where(ZoneRuntimeEvent.ended_at.is_(None))
            ).scalars()
        }
        started = extended = closed = 0
        threshold = gap_threshold_seconds(previous_nextpoll)

        for observation in observations:
            zone = zones.get(observation.relay_id)
            if zone is None:
                continue
            event = open_events.get(zone.id)

            if observation.is_running:
                if event is None:
                    decision = plan_start(
                        observation,
                        observed_at=observed_at,
                        previous_planned_seconds=previous_planned.get(observation.relay_id),
                        previous_nextpoll_seconds=previous_nextpoll,
                    )
                    db.add(
                        ZoneRuntimeEvent(
                            zone_id=zone.id,
                            started_at=decision.started_at,
                            last_running_at=observed_at,
                            planned_runtime_seconds=decision.planned_runtime_seconds,
                            last_remaining_seconds=decision.last_remaining_seconds,
                            confidence=decision.confidence,
                            source=EventSource.API_OBSERVED,
                            start_sample_id=sample_id,
                            flow_rate_lpm_snapshot=zone.flow_rate_lpm,
                            flow_min_lpm_snapshot=zone.flow_min_lpm,
                            flow_max_lpm_snapshot=zone.flow_max_lpm,
                        )
                    )
                    started += 1
                    logger.info(
                        "event.started",
                        extra={"zone": zone.label, "confidence": decision.confidence.value},
                    )
                else:
                    update = plan_extend(
                        _state_of(event), observation, observed_at=observed_at
                    )
                    event.last_running_at = update.last_running_at
                    event.last_remaining_seconds = update.last_remaining_seconds
                    event.planned_runtime_seconds = update.planned_runtime_seconds
                    extended += 1
            elif event is not None:
                gap = (
                    event.last_running_at is not None
                    and (observed_at - event.last_running_at).total_seconds() > threshold
                )
                closing = plan_close(
                    _state_of(event), observed_at=observed_at, gap_since_last_seen=gap
                )
                self._finalise(event, closing, zone, sample_id)
                closed += 1
                logger.info(
                    "event.finished",
                    extra={"zone": zone.label, "runtime_seconds": closing.runtime_seconds},
                )

        db.flush()
        return started, extended, closed

    def _finalise(
        self,
        event: ZoneRuntimeEvent,
        decision: CloseRun,
        zone: Zone,
        sample_id: int | None,
    ) -> None:
        """يغلق الحدث ويجمّد معدل التدفق المستخدم في حسابه (SRS §9.6)."""
        event.ended_at = decision.ended_at
        event.runtime_seconds = decision.runtime_seconds
        event.confidence = decision.confidence
        event.end_sample_id = sample_id
        if event.flow_rate_lpm_snapshot is None:
            event.flow_rate_lpm_snapshot = zone.flow_rate_lpm
            event.flow_min_lpm_snapshot = zone.flow_min_lpm
            event.flow_max_lpm_snapshot = zone.flow_max_lpm
        minutes = Decimal(decision.runtime_seconds) / Decimal(60)
        event.water_liters_estimate = (
            minutes * (event.flow_rate_lpm_snapshot or Decimal(0))
        ).quantize(Decimal("0.01"))

    # ------------------------------------------------------------------
    def close_stale_events(self, db: Session, *, now: datetime | None = None) -> int:
        """يغلق الأحداث العالقة بعد انقطاع أو إعادة تشغيل (SRS §17، §20)."""
        moment = now or utcnow()
        closed = 0
        events = db.execute(
            select(ZoneRuntimeEvent).where(ZoneRuntimeEvent.ended_at.is_(None))
        ).scalars()
        threshold = gap_threshold_seconds(self.settings.hydrawise_default_poll_seconds)
        for event in events:
            last_seen = event.last_running_at or event.started_at
            # لا يُعدّ الحدث عالقًا قبل أن يمضي وقته المتبقي المعلوم ثم فترة
            # صمت كاملة فوقه — وإلا أغلقنا ريًّا ما زال جاريًا.
            expected_end = last_seen + timedelta(seconds=event.last_remaining_seconds or 0)
            if (moment - expected_end).total_seconds() <= threshold:
                continue
            decision = plan_stale_close(_state_of(event), now=moment)
            self._finalise(event, decision, event.zone, None)
            event.source = EventSource.API_INFERRED
            closed += 1
            logger.info(
                "event.stale_closed",
                extra={"zone": event.zone.label, "runtime_seconds": decision.runtime_seconds},
            )
        return closed

    def purge_old_samples(self, db: Session, *, now: datetime | None = None) -> int:
        """يحذف العينات الخام بعد مدة الاحتفاظ — الأحداث والتقارير تبقى."""
        cutoff = (now or utcnow()) - timedelta(
            days=self.settings.hydrawise_raw_sample_retention_days
        )
        deleted = (
            db.query(PollSample).filter(PollSample.observed_at < cutoff).delete()
        )
        if deleted:
            logger.info("collector.samples_purged", extra={"deleted": deleted})
        return int(deleted or 0)

    # ------------------------------------------------------------------
    # الفجوات
    # ------------------------------------------------------------------
    def _open_gap(
        self,
        db: Session,
        controller: Controller,
        started_at: datetime,
        reason: GapReason,
        notes: str | None = None,
    ) -> DataGap | None:
        existing = db.execute(
            select(DataGap)
            .where(DataGap.controller_id == controller.id)
            .where(DataGap.ended_at.is_(None))
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        has_open_event = db.execute(
            select(ZoneRuntimeEvent.id)
            .join(Zone, Zone.id == ZoneRuntimeEvent.zone_id)
            .where(Zone.controller_id == controller.id)
            .where(ZoneRuntimeEvent.ended_at.is_(None))
            .limit(1)
        ).first() is not None
        gap = DataGap(
            controller_id=controller.id,
            started_at=started_at,
            reason=reason,
            may_affect_runtime=has_open_event,
            notes=notes,
        )
        db.add(gap)
        db.flush()
        logger.info("gap.opened", extra={"reason": reason.value})
        return gap

    def _close_open_gap(
        self, db: Session, controller: Controller, ended_at: datetime
    ) -> DataGap | None:
        gap = db.execute(
            select(DataGap)
            .where(DataGap.controller_id == controller.id)
            .where(DataGap.ended_at.is_(None))
        ).scalar_one_or_none()
        if gap is None:
            return None
        gap.ended_at = ended_at
        gap.duration_seconds = max(0, int((ended_at - gap.started_at).total_seconds()))
        logger.info("gap.closed", extra={"duration_seconds": gap.duration_seconds})
        return gap

    def record_worker_downtime(
        self, db: Session, controller: Controller, *, now: datetime | None = None
    ) -> DataGap | None:
        """يوثّق فترة توقف الـWorker عند بدء التشغيل (SRS §20).

        بدون هذا تُحسب فترة التوقف كتغطية سليمة، فيبدو تقرير ناقص كأنه كامل.
        """
        moment = now or utcnow()
        last = db.execute(
            select(PollSample.observed_at)
            .where(PollSample.controller_id == controller.id)
            .where(PollSample.is_success.is_(True))
            .order_by(PollSample.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            return None
        silence = (moment - last).total_seconds()
        if silence <= gap_threshold_seconds(self.settings.hydrawise_default_poll_seconds):
            return None
        gap = DataGap(
            controller_id=controller.id,
            started_at=last,
            ended_at=moment,
            reason=GapReason.WORKER_DOWN,
            duration_seconds=int(silence),
            may_affect_runtime=True,
            notes="توقف الجامع بين آخر عينة ناجحة وبدء التشغيل",
        )
        db.add(gap)
        logger.info("gap.worker_down", extra={"duration_seconds": int(silence)})
        return gap

    # ------------------------------------------------------------------
    def _handle_failure(
        self,
        db: Session,
        controller: Controller,
        observed_at: datetime,
        reason: GapReason,
        code: str,
        message: str,
        *,
        fatal: bool = False,
        next_seconds: int | None = None,
    ) -> PollOutcome:
        db.add(
            PollSample(
                controller_id=controller.id,
                observed_at=observed_at,
                is_success=False,
                error_code=code,
                nextpoll_seconds=None,
            )
        )
        self._open_gap(db, controller, observed_at, reason, notes=code)
        streak = self._failure_streak.get(controller.id, 0)
        self._failure_streak[controller.id] = streak + 1
        delay = next_seconds or NETWORK_BACKOFF_SECONDS[
            min(streak, len(NETWORK_BACKOFF_SECONDS) - 1)
        ]
        logger.warning(
            "collector.poll_failed",
            extra={"error_code": code, "retry_in": delay, "fatal": fatal},
        )
        return PollOutcome(
            controller_id=controller.id,
            ok=False,
            next_poll_seconds=int(delay),
            observed_at=observed_at,
            error_code=code,
            error_message=message,
            fatal=fatal,
        )

    def _previous_successful_sample(
        self, db: Session, controller: Controller, current_sample_id: int
    ) -> PollSample | None:
        return db.execute(
            select(PollSample)
            .where(PollSample.controller_id == controller.id)
            .where(PollSample.is_success.is_(True))
            .where(PollSample.id != current_sample_id)
            .order_by(PollSample.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _planned_from_sample(sample: PollSample | None) -> dict[int, int]:
        """المدة المخططة لكل محبس كما أعلنتها العينة السابقة.

        عند التوقف تحمل ``relay.run`` مدة التشغيل القادم؛ هي ما يسمح
        بتقدير كم مضى من التشغيل عندما نراه لأول مرة (SRS §9.4).
        """
        if sample is None or not sample.payload:
            return {}
        try:
            payload = StatusSchedulePayload.model_validate(sample.payload)
        except Exception:  # عينة قديمة بصيغة غير متوقعة
            return {}
        planned: dict[int, int] = {}
        for observation in observations_from_payload(payload):
            if observation.planned_seconds:
                planned[observation.relay_id] = observation.planned_seconds
        return planned


def _state_of(event: ZoneRuntimeEvent) -> OpenRunState:
    return OpenRunState(
        started_at=event.started_at,
        last_running_at=event.last_running_at or event.started_at,
        planned_runtime_seconds=event.planned_runtime_seconds,
        last_remaining_seconds=event.last_remaining_seconds,
        confidence=event.confidence or Confidence.MEDIUM,
    )
