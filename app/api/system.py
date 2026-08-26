"""الصحة وحالة الجامع واختبار التكاملات (SRS §FR-001، §NFR-005، §13)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_admin, verify_csrf
from app.core.config import get_settings
from app.core.logging import get_logger, mask_phone
from app.core.templating import page
from app.core.time import utcnow
from app.db.session import database_ready, get_db
from app.models import Controller, DataGap
from app.schemas.api import CollectorStatusOut, HealthOut
from app.services import audit
from app.services.hydrawise_client import HydrawiseClient, HydrawiseError
from app.services.status import controller_status

router = APIRouter(tags=["system"])
logger = get_logger(__name__)

GAP_LABELS = {
    "network": "انقطاع شبكة",
    "api_429": "تجاوز حد الطلبات",
    "api_error": "خطأ من الـAPI",
    "invalid_payload": "استجابة غير صالحة",
    "worker_down": "توقف الجامع",
}


@router.get("/api/v1/health", response_model=HealthOut)
def health() -> HealthOut:
    """فحص لا يتطلب تسجيل دخول — يستخدمه Docker والمراقبة."""
    settings = get_settings()
    ok = database_ready()
    return HealthOut(
        status="ok" if ok else "degraded",
        database=ok,
        hydrawise_configured=settings.hydrawise_configured,
        openwa_configured=settings.openwa_configured,
    )


@router.get("/api/v1/system/collector-status", response_model=list[CollectorStatusOut])
def collector_status(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> list[CollectorStatusOut]:
    now = utcnow()
    result = []
    for controller in db.execute(select(Controller)).scalars():
        status = controller_status(db, controller, now=now)
        result.append(
            CollectorStatusOut(
                controller=controller.name,
                last_successful_poll_at=controller.last_successful_poll_at,
                seconds_since_poll=status.seconds_since_poll,
                healthy=status.online,
                open_gap=status.gap_open,
                samples_last_24h=status.samples_last_24h,
                open_events=status.open_events,
            )
        )
    return result


@router.get("/system/health")
def health_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    message: str | None = None,
    error: str | None = None,
) -> Response:
    settings = get_settings()
    now = utcnow()
    collectors = []
    for controller in db.execute(select(Controller).order_by(Controller.name)).scalars():
        status = controller_status(db, controller, now=now)
        state, tone = status.state_label
        collectors.append(
            {
                "name": controller.name,
                "last_poll_at": controller.last_successful_poll_at,
                "seconds_since": status.seconds_since_poll,
                "state": state,
                "tone": tone,
                "samples_24h": status.samples_last_24h,
            }
        )
    gaps = list(
        db.execute(select(DataGap).order_by(DataGap.started_at.desc()).limit(20)).scalars()
    )
    return page(
        request,
        "health.html",
        {
            "user": user,
            "active": "health",
            "health": {
                "database": database_ready(),
                "hydrawise_configured": settings.hydrawise_configured,
                "openwa_configured": settings.openwa_configured,
                "openwa_recipient": mask_phone(settings.openwa_recipient)
                if settings.openwa_recipient
                else "",
            },
            "collectors": collectors,
            "gaps": gaps,
            "gap_labels": GAP_LABELS,
            "audit_entries": audit.recent(db, limit=15),
            "message": message,
            "error": error,
        },
    )


# ----------------------------------------------------------------------
async def _test_connection(db: Session, actor: str) -> tuple[bool, str]:
    settings = get_settings()
    if not settings.hydrawise_configured:
        return False, "لا يوجد مفتاح Hydrawise في البيئة"
    try:
        details, _raw = await HydrawiseClient.from_settings().customer_details()
    except HydrawiseError as exc:
        audit.record(db, actor=actor, action="hydrawise.test_failed", reason=str(exc))
        return False, str(exc)
    names = "، ".join(
        item.name or str(item.controller_id) for item in details.controllers
    )
    audit.record(db, actor=actor, action="hydrawise.test_ok")
    return True, f"الاتصال ناجح — كنترولرات: {names or 'واحد'}"


@router.post("/api/v1/integrations/hydrawise/test", dependencies=[Depends(verify_csrf)])
async def test_connection_api(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict[str, object]:
    ok, detail = await _test_connection(db, user.username)
    return {"ok": ok, "detail": detail}


@router.post("/system/test-connection", dependencies=[Depends(verify_csrf)])
async def test_connection_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    ok, detail = await _test_connection(db, user.username)
    return health_page(
        request, db, user, message=detail if ok else None, error=None if ok else detail
    )


async def _sync(db: Session, actor: str) -> tuple[bool, str]:
    from app.services.collector import Collector

    settings = get_settings()
    if not settings.hydrawise_configured:
        return False, "لا يوجد مفتاح Hydrawise في البيئة"
    collector = Collector(HydrawiseClient.from_settings())
    try:
        controllers = await collector.sync_controllers(db)
        for controller in controllers:
            await collector.poll(db, controller)
    except HydrawiseError as exc:
        audit.record(db, actor=actor, action="hydrawise.sync_failed", reason=str(exc))
        return False, str(exc)
    audit.record(db, actor=actor, action="hydrawise.sync_ok")
    return True, f"تمت مزامنة {len(controllers)} كنترولر واستطلاعها."


@router.post("/api/v1/integrations/hydrawise/sync", dependencies=[Depends(verify_csrf)])
async def sync_api(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict[str, object]:
    ok, detail = await _sync(db, user.username)
    return {"ok": ok, "detail": detail}


@router.post("/system/sync", dependencies=[Depends(verify_csrf)])
async def sync_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    ok, detail = await _sync(db, user.username)
    return health_page(
        request, db, user, message=detail if ok else None, error=None if ok else detail
    )


@router.get("/api/v1/controllers")
def list_controllers(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> list[dict[str, object]]:
    return [
        {
            "id": str(controller.id),
            "hydrawise_controller_id": controller.hydrawise_controller_id,
            "name": controller.name,
            "timezone": controller.timezone,
            "is_active": controller.is_active,
            "serial_masked": controller.masked_serial,
            "last_successful_poll_at": (
                controller.last_successful_poll_at.isoformat()
                if controller.last_successful_poll_at
                else None
            ),
        }
        for controller in db.execute(select(Controller).order_by(Controller.name)).scalars()
    ]
