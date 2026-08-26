"""لوحة الحالة الحالية وملخص اليوم (SRS §FR-005، §FR-006)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user
from app.core.templating import page
from app.core.time import to_local, utcnow
from app.db.session import get_db
from app.models import Controller
from app.services.report_generator import generate_daily_summary
from app.services.status import controller_status

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    now = utcnow()
    controllers = list(
        db.execute(select(Controller).order_by(Controller.name)).scalars()
    )
    items = []
    for controller in controllers:
        status = controller_status(db, controller, now=now)
        today = to_local(now, controller.timezone).date()
        items.append(
            {
                "controller": controller,
                "online": status.online,
                "seconds_since_poll": status.seconds_since_poll,
                "gap_open": status.gap_open,
                "zones": status.zones,
                "running": status.running,
                "today": generate_daily_summary(db, controller, day=today, now=now),
            }
        )
    site_name = controllers[0].name if controllers else None
    return page(
        request,
        "dashboard.html",
        {"user": user, "controllers": items, "active": "dashboard", "site_name": site_name},
    )
