"""الرابط العام للتقرير — قراءة فقط، برمز مؤقت (SRS §12.1، §NFR-003)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.exports import render_report_html
from app.services.sharing import resolve_share_token

router = APIRouter(tags=["public"])


@router.get("/r/{token}", response_class=HTMLResponse)
def public_report(
    token: str, request: Request, db: Annotated[Session, Depends(get_db)]
) -> HTMLResponse:
    """يعرض تقريرًا بدون تسجيل دخول إن كان الرمز صالحًا وغير منتهٍ.

    الرمز غير الصالح والمنتهي يعطيان نفس الرد، حتى لا يكشف الفرق بينهما
    وجود تقرير من عدمه.
    """
    report = resolve_share_token(db, token)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="الرابط غير صالح أو انتهت صلاحيته",
        )
    html = render_report_html(
        report.summary_json,
        request=request,
        user=None,
        show_actions=False,
        standalone=True,
    )
    # لا يُفهرس ولا يُخزَّن في وسيط: الرابط سرّي وإن كان بلا تسجيل دخول.
    return HTMLResponse(
        html,
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store, private",
        },
    )
