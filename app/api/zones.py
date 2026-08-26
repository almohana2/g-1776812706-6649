"""المحابس ومعايرة التدفق (SRS §FR-012، §13)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_admin, verify_csrf
from app.core.templating import page
from app.core.time import utcnow
from app.db.session import get_db
from app.models import CalibrationMethod, Zone
from app.schemas.api import ZoneOut, ZoneUpdateIn
from app.services import audit

router = APIRouter(tags=["zones"])

METHOD_LABELS = {
    "default": "افتراضي (غير معاير)",
    "manual": "قياس يدوي",
    "flow_meter": "عداد تدفق",
    "pump_curve": "منحنى المضخة",
}


def _zones(db: Session) -> list[Zone]:
    return list(
        db.execute(
            select(Zone).order_by(Zone.physical_number, Zone.hydrawise_relay_id)
        ).scalars()
    )


@router.get("/zones")
def zones_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    message: str | None = None,
    error: str | None = None,
) -> Response:
    return page(
        request,
        "zones.html",
        {
            "user": user,
            "zones": _zones(db),
            "active": "zones",
            "calibration_methods": list(CalibrationMethod),
            "method_labels": METHOD_LABELS,
            "message": message,
            "error": error,
        },
    )


def _apply_update(
    db: Session, zone: Zone, payload: ZoneUpdateIn, *, actor: str
) -> None:
    """يطبّق التعديل ويكتب قبل/بعد في سجل التدقيق (SRS §FR-012)."""
    if not payload.flow_bounds_ok(
        float(zone.flow_min_lpm), float(zone.flow_rate_lpm), float(zone.flow_max_lpm)
    ):
        raise ValueError("يجب أن يكون: أدنى تدفق ≤ المرجح ≤ الأعلى")

    before = {
        "display_name_ar": zone.display_name_ar,
        "flow_rate_lpm": float(zone.flow_rate_lpm),
        "flow_min_lpm": float(zone.flow_min_lpm),
        "flow_max_lpm": float(zone.flow_max_lpm),
        "calibration_method": zone.calibration_method.value,
    }
    if payload.display_name_ar is not None:
        zone.display_name_ar = payload.display_name_ar.strip() or None
    if payload.flow_rate_lpm is not None:
        zone.flow_rate_lpm = Decimal(str(payload.flow_rate_lpm))
    if payload.flow_min_lpm is not None:
        zone.flow_min_lpm = Decimal(str(payload.flow_min_lpm))
    if payload.flow_max_lpm is not None:
        zone.flow_max_lpm = Decimal(str(payload.flow_max_lpm))
    if payload.calibration_method is not None:
        zone.calibration_method = payload.calibration_method
        if payload.calibration_method is not CalibrationMethod.DEFAULT:
            zone.calibrated_at = utcnow()

    after = {
        "display_name_ar": zone.display_name_ar,
        "flow_rate_lpm": float(zone.flow_rate_lpm),
        "flow_min_lpm": float(zone.flow_min_lpm),
        "flow_max_lpm": float(zone.flow_max_lpm),
        "calibration_method": zone.calibration_method.value,
    }
    audit.record(
        db,
        actor=actor,
        action="zone.updated",
        entity_type="zone",
        entity_id=str(zone.id),
        reason=payload.reason,
        before=before,
        after=after,
    )


@router.post("/zones/{zone_id}", dependencies=[Depends(verify_csrf)])
def update_zone_form(
    zone_id: uuid.UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    flow_rate_lpm: Annotated[float, Form()],
    flow_min_lpm: Annotated[float, Form()],
    flow_max_lpm: Annotated[float, Form()],
    calibration_method: Annotated[str, Form()] = "default",
    display_name_ar: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "المحبس غير موجود")
    payload = ZoneUpdateIn(
        display_name_ar=display_name_ar,
        flow_rate_lpm=flow_rate_lpm,
        flow_min_lpm=flow_min_lpm,
        flow_max_lpm=flow_max_lpm,
        calibration_method=CalibrationMethod(calibration_method),
        reason=reason or None,
    )
    try:
        _apply_update(db, zone, payload, actor=user.username)
    except ValueError as exc:
        return page(
            request,
            "zones.html",
            {
                "user": user,
                "zones": _zones(db),
                "active": "zones",
                "calibration_methods": list(CalibrationMethod),
                "method_labels": METHOD_LABELS,
                "error": str(exc),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return page(
        request,
        "zones.html",
        {
            "user": user,
            "zones": _zones(db),
            "active": "zones",
            "calibration_methods": list(CalibrationMethod),
            "method_labels": METHOD_LABELS,
            "message": f"حُفظت إعدادات {zone.label}.",
        },
    )


# ----------------------------------------------------------------------
# واجهة JSON
# ----------------------------------------------------------------------
@router.get("/api/v1/zones", response_model=list[ZoneOut])
def list_zones(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> list[Zone]:
    return _zones(db)


@router.patch("/api/v1/zones/{zone_id}", response_model=ZoneOut)
def patch_zone(
    zone_id: uuid.UUID,
    payload: ZoneUpdateIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    __: Annotated[None, Depends(verify_csrf)],
) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "المحبس غير موجود")
    try:
        _apply_update(db, zone, payload, actor=user.username)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return zone
