"""سجل التشغيل: عرض، تصفية، وتعديل يدوي مضبوط (SRS §FR-013، §13)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_admin, verify_csrf
from app.core.templating import page
from app.core.time import local_day_bounds, to_utc
from app.db.session import get_db
from app.models import Confidence, EventSource, Zone, ZoneRuntimeEvent
from app.schemas.api import EventCreateIn, EventOut, EventUpdateIn
from app.services import audit

router = APIRouter(tags=["events"])

CONFIDENCE_LABELS = {"high": "عالية", "medium": "متوسطة", "low": "منخفضة"}
MAX_PAGE = 500


def _filtered(
    db: Session,
    *,
    zone_id: str | None,
    date_from: str | None,
    date_to: str | None,
    confidence: str | None,
) -> tuple[Select[tuple[ZoneRuntimeEvent]], Select[tuple[int]]]:
    """يبني استعلام السرد واستعلام العدّ بنفس المرشّحات."""
    query = select(ZoneRuntimeEvent).join(Zone, Zone.id == ZoneRuntimeEvent.zone_id)
    count_query = select(func.count(ZoneRuntimeEvent.id)).join(
        Zone, Zone.id == ZoneRuntimeEvent.zone_id
    )
    if zone_id:
        try:
            key = uuid.UUID(zone_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "معرّف محبس غير صالح") from exc
        query = query.where(ZoneRuntimeEvent.zone_id == key)
        count_query = count_query.where(ZoneRuntimeEvent.zone_id == key)
    if date_from:
        start, _ = local_day_bounds(date.fromisoformat(date_from))
        query = query.where(ZoneRuntimeEvent.started_at >= start)
        count_query = count_query.where(ZoneRuntimeEvent.started_at >= start)
    if date_to:
        _, end = local_day_bounds(date.fromisoformat(date_to))
        query = query.where(ZoneRuntimeEvent.started_at < end)
        count_query = count_query.where(ZoneRuntimeEvent.started_at < end)
    if confidence:
        level = Confidence(confidence)
        query = query.where(ZoneRuntimeEvent.confidence == level)
        count_query = count_query.where(ZoneRuntimeEvent.confidence == level)
    return query, count_query


@router.get("/events")
def events_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    zone: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    confidence: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=MAX_PAGE),
) -> Response:
    try:
        query, count_query = _filtered(
            db, zone_id=zone, date_from=date_from, date_to=date_to, confidence=confidence
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    events = list(
        db.execute(query.order_by(ZoneRuntimeEvent.started_at.desc()).limit(limit)).scalars()
    )
    total = int(db.execute(count_query).scalar_one())
    zones = list(db.execute(select(Zone).order_by(Zone.physical_number)).scalars())
    return page(
        request,
        "events.html",
        {
            "user": user,
            "events": events,
            "total": total,
            "zones": zones,
            "active": "events",
            "confidences": list(Confidence),
            "confidence_labels": CONFIDENCE_LABELS,
            "filters": {
                "zone_id": zone,
                "date_from": date_from,
                "date_to": date_to,
                "confidence": confidence,
            },
        },
    )


# ----------------------------------------------------------------------
@router.get("/api/v1/events", response_model=list[EventOut])
def list_events(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
    zone: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    confidence: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=MAX_PAGE),
) -> list[ZoneRuntimeEvent]:
    query, _count = _filtered(
        db, zone_id=zone, date_from=date_from, date_to=date_to, confidence=confidence
    )
    return list(
        db.execute(query.order_by(ZoneRuntimeEvent.started_at.desc()).limit(limit)).scalars()
    )


def _recompute_water(event: ZoneRuntimeEvent, zone: Zone) -> None:
    flow = event.flow_rate_lpm_snapshot or zone.flow_rate_lpm
    seconds = event.runtime_seconds or 0
    event.flow_rate_lpm_snapshot = flow
    event.water_liters_estimate = (
        Decimal(seconds) / Decimal(60) * flow
    ).quantize(Decimal("0.01"))


@router.post(
    "/api/v1/events", response_model=EventOut, status_code=status.HTTP_201_CREATED
)
def create_event(
    payload: EventCreateIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    _: Annotated[None, Depends(verify_csrf)],
) -> ZoneRuntimeEvent:
    """إضافة حدث يدويًا — بسبب إلزامي ووسم مصدر واضح."""
    zone = db.get(Zone, payload.zone_id)
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "المحبس غير موجود")
    started = to_utc(payload.started_at)
    ended = to_utc(payload.ended_at)
    event = ZoneRuntimeEvent(
        zone_id=zone.id,
        started_at=started,
        ended_at=ended,
        last_running_at=ended,
        runtime_seconds=int((ended - started).total_seconds()),
        source=EventSource.MANUAL_ADJUSTMENT,
        confidence=Confidence.LOW,
        is_adjusted=True,
        adjustment_reason=payload.reason,
        flow_rate_lpm_snapshot=zone.flow_rate_lpm,
        flow_min_lpm_snapshot=zone.flow_min_lpm,
        flow_max_lpm_snapshot=zone.flow_max_lpm,
    )
    _recompute_water(event, zone)
    db.add(event)
    db.flush()
    audit.record(
        db, actor=user.username, action="event.created_manually",
        entity_type="zone_runtime_event", entity_id=str(event.id),
        reason=payload.reason,
        after={"started_at": started.isoformat(), "ended_at": ended.isoformat()},
    )
    return event


@router.patch("/api/v1/events/{event_id}", response_model=EventOut)
def update_event(
    event_id: uuid.UUID,
    payload: EventUpdateIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    _: Annotated[None, Depends(verify_csrf)],
) -> ZoneRuntimeEvent:
    """تعديل أو استبعاد حدث — يُحفظ قبل/بعد والسبب في سجل التدقيق."""
    event = db.get(ZoneRuntimeEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "الحدث غير موجود")
    before = {
        "started_at": event.started_at.isoformat(),
        "ended_at": event.ended_at.isoformat() if event.ended_at else None,
        "runtime_seconds": event.runtime_seconds,
    }
    if payload.exclude:
        # الاستبعاد لا يحذف السجل: يصفّر مدته ويوسمه معدَّلًا حتى يبقى أثره.
        event.runtime_seconds = 0
        event.water_liters_estimate = Decimal("0")
        event.ended_at = event.ended_at or event.started_at
    else:
        if payload.started_at is not None:
            event.started_at = to_utc(payload.started_at)
        if payload.ended_at is not None:
            event.ended_at = to_utc(payload.ended_at)
        if event.ended_at is not None:
            if event.ended_at < event.started_at:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "النهاية قبل البداية"
                )
            event.runtime_seconds = int(
                (event.ended_at - event.started_at).total_seconds()
            )
        _recompute_water(event, event.zone)
    event.is_adjusted = True
    event.adjustment_reason = payload.reason
    event.source = EventSource.MANUAL_ADJUSTMENT
    db.flush()
    audit.record(
        db, actor=user.username, action="event.adjusted",
        entity_type="zone_runtime_event", entity_id=str(event.id),
        reason=payload.reason, before=before,
        after={
            "started_at": event.started_at.isoformat(),
            "ended_at": event.ended_at.isoformat() if event.ended_at else None,
            "runtime_seconds": event.runtime_seconds,
        },
    )
    return event
