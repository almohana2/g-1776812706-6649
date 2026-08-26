"""تسجيل الدخول والخروج مع تحديد معدل المحاولات (SRS §NFR-003)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    clear_session,
    current_user,
    issue_session,
    optional_user,
    user_by_username,
    verify_csrf,
)
from app.core.logging import get_logger
from app.core.security import RateLimiter, hash_password, needs_rehash, verify_password
from app.core.templating import page
from app.core.time import utcnow
from app.db.session import get_db
from app.services import audit

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)

# خمس محاولات لكل عنوان/اسم خلال خمس دقائق.
_login_limiter = RateLimiter(max_events=5, window_seconds=300)

GENERIC_ERROR = "بيانات الدخول غير صحيحة."


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(
    request: Request, user: Annotated[CurrentUser | None, Depends(optional_user)]
) -> Response:
    if user is not None:
        return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return page(request, "login.html", {"user": None})


@router.post("/login")
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    ip = _client_ip(request)
    limiter_key = f"{ip}:{username.strip().lower()}"
    if not _login_limiter.hit(limiter_key):
        audit.record(
            db, actor=username[:80], action="login.rate_limited", client_ip=ip
        )
        return page(
            request,
            "login.html",
            {"user": None, "error": "محاولات كثيرة. انتظر خمس دقائق ثم أعد المحاولة."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = user_by_username(db, username)
    # يُتحقق من كلمة المرور دائمًا برسالة واحدة، حتى لا يُكشف وجود الحساب.
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        audit.record(db, actor=username[:80], action="login.failed", client_ip=ip)
        return page(
            request,
            "login.html",
            {"user": None, "error": GENERIC_ERROR},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    _login_limiter.reset(limiter_key)
    audit.record(
        db, actor=user.username, action="login.success", client_ip=ip,
        entity_type="user", entity_id=str(user.id),
    )

    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    issue_session(response, user)
    return response


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(user: Annotated[CurrentUser, Depends(current_user)]) -> Response:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session(response)
    logger.info("logout", extra={"actor": user.username})
    return response
