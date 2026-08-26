"""التقارير الشهرية: عرض، توليد، تصدير، مشاركة، إرسال (SRS §FR-007..FR-011)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_admin, verify_csrf
from app.core.logging import get_logger
from app.core.templating import page
from app.core.time import month_key, parse_month_key, previous_month
from app.db.session import get_db
from app.models import Controller, MonthlyReport
from app.services import audit
from app.services.exports import (
    PdfUnavailable,
    render_report_html,
    render_report_pdf,
    render_zones_csv,
    report_filename,
)
from app.services.report_generator import generate_monthly_report
from app.services.sharing import issue_share_token, public_url

router = APIRouter(tags=["reports"])
logger = get_logger(__name__)

STATUS_LABELS = {
    "draft": "مسودة",
    "final": "نهائي",
    "sent": "أُرسل",
    "failed": "فشل الإرسال",
}


def _primary_controller(db: Session) -> Controller:
    controller = db.execute(
        select(Controller).where(Controller.is_active.is_(True)).order_by(Controller.name)
    ).scalars().first()
    if controller is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "لم يُكتشف أي كنترولر بعد — شغّل المزامنة أولًا",
        )
    return controller


def _report_or_404(db: Session, month: str) -> MonthlyReport:
    try:
        year, number = parse_month_key(month)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    report = db.execute(
        select(MonthlyReport)
        .where(MonthlyReport.month == date(year, number, 1))
        .order_by(MonthlyReport.generated_at.desc())
    ).scalars().first()
    if report is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"لا يوجد تقرير لشهر {month} — ولّده أولًا"
        )
    return report


def _generate(db: Session, month: str, *, actor: str) -> MonthlyReport:
    year, number = parse_month_key(month)
    controller = _primary_controller(db)
    report = generate_monthly_report(db, controller, year=year, month=number)
    audit.record(
        db, actor=actor, action="report.generated", entity_type="monthly_report",
        entity_id=str(report.id), after={"month": month},
    )
    return report


# ----------------------------------------------------------------------
# الصفحات
# ----------------------------------------------------------------------
@router.get("/reports")
def reports_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    message: str | None = None,
    error: str | None = None,
) -> Response:
    reports = list(
        db.execute(select(MonthlyReport).order_by(MonthlyReport.month.desc())).scalars()
    )
    year, number = previous_month()
    return page(
        request,
        "reports_list.html",
        {
            "user": user,
            "reports": reports,
            "active": "reports",
            "status_labels": STATUS_LABELS,
            "default_month": month_key(year, number),
            "message": message,
            "error": error,
        },
    )


@router.get("/reports/{month}")
def report_page(
    month: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(current_user)],
    share: str | None = None,
) -> Response:
    report = _report_or_404(db, month)
    html = render_report_html(
        report.summary_json,
        request=request,
        user=user,
        show_actions=True,
        share_url=public_url(share) if share else None,
        share_expires=report.public_token_expires_at if share else None,
        deliveries=list(report.deliveries),
    )
    return HTMLResponse(html)


@router.post("/reports/generate", dependencies=[Depends(verify_csrf)])
def generate_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    month: Annotated[str, Form()],
) -> Response:
    try:
        _generate(db, month.strip(), actor=user.username)
    except (ValueError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return reports_page(request, db, user, error=str(detail))
    return reports_page(request, db, user, message=f"تم توليد تقرير {month}.")


@router.post("/reports/{month}/regenerate", dependencies=[Depends(verify_csrf)])
def regenerate_form(
    month: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    _generate(db, month, actor=user.username)
    return reports_page(request, db, user, message=f"أُعيد توليد تقرير {month}.")


@router.post("/reports/{month}/share", dependencies=[Depends(verify_csrf)])
def share_form(
    month: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    report = _report_or_404(db, month)
    token = issue_share_token(report)
    expires_at = report.public_token_expires_at
    audit.record(
        db, actor=user.username, action="report.share_link_issued",
        entity_type="monthly_report", entity_id=str(report.id),
        after={"expires_at": expires_at.isoformat() if expires_at else None},
    )
    return report_page(month, request, db, user, share=token)


@router.post("/reports/{month}/send", dependencies=[Depends(verify_csrf)])
def send_form(
    month: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> Response:
    from app.services.openwa_client import send_report

    report = _report_or_404(db, month)
    result = send_report(db, report, actor=user.username)
    if result.ok:
        return reports_page(
            request, db, user, message=f"أُرسل تقرير {month} إلى {result.recipient_masked}."
        )
    return reports_page(request, db, user, error=f"تعذّر الإرسال: {result.detail}")


# ----------------------------------------------------------------------
# واجهة JSON والتصدير
# ----------------------------------------------------------------------
@router.get("/api/v1/reports/monthly/{month}")
def report_json(
    month: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> JSONResponse:
    report = _report_or_404(db, month)
    return JSONResponse(report.summary_json)


@router.post(
    "/api/v1/reports/monthly/{month}/generate", status_code=status.HTTP_201_CREATED
)
def report_generate(
    month: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    _: Annotated[None, Depends(verify_csrf)],
) -> JSONResponse:
    report = _generate(db, month, actor=user.username)
    return JSONResponse(report.summary_json, status_code=status.HTTP_201_CREATED)


@router.get("/api/v1/reports/monthly/{month}/pdf")
def report_pdf(
    month: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    report = _report_or_404(db, month)
    try:
        pdf = render_report_pdf(report.summary_json)
    except PdfUnavailable as exc:
        # فشل PDF لا يمنع الاطلاع: صفحة HTML تبقى متاحة (SRS §20).
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"تعذّر توليد PDF ({exc}). صفحة التقرير متاحة على /reports/{month}",
        ) from exc
    name = report_filename(report.controller.name, report.month_key, "report", "pdf")
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/api/v1/reports/monthly/{month}/csv")
def report_csv(
    month: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[CurrentUser, Depends(current_user)],
) -> Response:
    report = _report_or_404(db, month)
    name = report_filename(report.controller.name, report.month_key, "zones", "csv")
    return Response(
        render_zones_csv(report.summary_json),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/v1/reports/monthly/{month}/send")
def report_send(
    month: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_admin)],
    _: Annotated[None, Depends(verify_csrf)],
) -> JSONResponse:
    from app.services.openwa_client import send_report

    report = _report_or_404(db, month)
    result = send_report(db, report, actor=user.username)
    return JSONResponse(
        {
            "status": result.status.value,
            "detail": result.detail,
            "recipient_masked": result.recipient_masked,
            "provider_message_id": result.provider_message_id,
        },
        status_code=200 if result.ok else 502,
    )
