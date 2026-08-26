"""صفحات الإعدادات: التكاملات وبيانات المضخة (SRS §12.1، §FR-001)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_admin, verify_csrf
from app.core.config import get_settings
from app.core.logging import mask_phone
from app.core.templating import page
from app.db.session import get_db
from app.models import Controller, PumpProfile
from app.services import audit

router = APIRouter(tags=["settings"])


@router.get("/settings/integrations")
def integrations_page(
    request: Request,
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    """يعرض حالة التكاملات دون كشف أي سر (SRS §FR-001، AC-011)."""
    settings = get_settings()
    return page(
        request,
        "settings_integrations.html",
        {
            "user": user,
            "active": "settings",
            "hydrawise": {
                "configured": settings.hydrawise_configured,
                "base": settings.hydrawise_api_base,
                "key_env": "HYDRAWISE_API_KEY",
                "controller_id": settings.hydrawise_controller_id,
                "timeout": settings.hydrawise_http_timeout_seconds,
                "retention_days": settings.hydrawise_raw_sample_retention_days,
            },
            "openwa": {
                "enabled": settings.openwa_enabled,
                "configured": settings.openwa_configured,
                "base": settings.openwa_base_url,
                "recipient": mask_phone(settings.openwa_recipient),
                "path": settings.openwa_send_path.replace(
                    "{session_id}", "{SESSION_ID}"
                ),
                "auth_header": settings.openwa_auth_header,
            },
            "report": {
                "timezone": settings.report_timezone,
                "monthly_cron": settings.monthly_report_cron,
                "daily_cron": settings.daily_report_cron,
                "link_ttl_days": settings.report_public_link_ttl_days,
            },
        },
    )


def _pump(db: Session) -> tuple[Controller, PumpProfile] | tuple[None, None]:
    controller = db.execute(
        select(Controller).order_by(Controller.name)
    ).scalars().first()
    if controller is None:
        return None, None
    profile = controller.pump_profile
    if profile is None:
        settings = get_settings()
        profile = PumpProfile(
            controller_id=controller.id,
            estimated_input_kw=Decimal(str(settings.pump_estimated_input_kw)),
        )
        db.add(profile)
        db.flush()
    return controller, profile


@router.get("/settings/pump")
def pump_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    message: str | None = None,
) -> Response:
    controller, profile = _pump(db)
    return page(
        request,
        "settings_pump.html",
        {
            "user": user,
            "active": "settings",
            "controller": controller,
            "pump": profile,
            "message": message,
        },
    )


@router.post("/settings/pump", dependencies=[Depends(verify_csrf)])
def pump_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    estimated_input_kw: Annotated[float, Form()],
    brand: Annotated[str, Form()] = "",
    model: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    """يغيّر القدرة المستخدمة في تقدير الطاقة — لا يمس أي إعداد تشغيلي."""
    controller, profile = _pump(db)
    if profile is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "لا يوجد كنترولر بعد")
    if estimated_input_kw <= 0 or estimated_input_kw > 100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "قدرة غير معقولة")
    before = {"estimated_input_kw": float(profile.estimated_input_kw)}
    profile.estimated_input_kw = Decimal(str(estimated_input_kw))
    profile.brand = brand.strip() or profile.brand
    profile.model = model.strip() or profile.model
    profile.notes = notes.strip() or profile.notes
    audit.record(
        db, actor=user.username, action="pump.updated", entity_type="pump_profile",
        entity_id=str(profile.id), reason=reason or None, before=before,
        after={"estimated_input_kw": float(profile.estimated_input_kw)},
    )
    return pump_page(request, db, user, message="حُفظت بيانات المضخة.")
