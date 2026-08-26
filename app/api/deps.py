"""اعتماديات مشتركة: الجلسة، الصلاحيات، حماية CSRF، الجلسة الحالية."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import SessionCodec, csrf_token_for, verify_csrf_token
from app.db.session import get_db
from app.models import User, UserRole

SESSION_COOKIE = "hir_session"
_codec = SessionCodec()


@dataclass(frozen=True)
class CurrentUser:
    """هوية الطالب الحالي، مع رمز CSRF الخاص بجلسته."""

    id: uuid.UUID
    username: str
    role: UserRole
    session_id: str

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    @property
    def csrf_token(self) -> str:
        return csrf_token_for(self.session_id)


def issue_session(response: Response, user: User) -> str:
    """يكتب كعكة الجلسة ويعيد معرّف الجلسة."""
    settings = get_settings()
    session_id = secrets.token_urlsafe(16)
    value = _codec.dumps(
        {
            "uid": str(user.id),
            "sid": session_id,
            "epoch": user.session_epoch,
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=_codec.max_age_seconds,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )
    return session_id


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _as_int(value: object) -> int:
    """قيمة الكعكة تأتي كـJSON: أي شيء غير رقم يُعامل كجلسة غير صالحة."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return -1


def _load_user(request: Request, db: Session) -> CurrentUser | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    data = _codec.loads(raw)
    if not data:
        return None
    try:
        user_id = uuid.UUID(str(data.get("uid")))
    except (ValueError, TypeError):
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    # تغيير كلمة المرور أو تعطيل الحساب يرفع الـepoch فتسقط الجلسات القديمة.
    if _as_int(data.get("epoch")) != user.session_epoch:
        return None
    return CurrentUser(
        id=user.id,
        username=user.username,
        role=user.role,
        session_id=str(data.get("sid", "")),
    )


def optional_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> CurrentUser | None:
    """المستخدم الحالي إن وُجد — تستخدمها الصفحات العامة."""
    return _load_user(request, db)


def current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> CurrentUser:
    """يفرض تسجيل الدخول."""
    user = _load_user(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="يلزم تسجيل الدخول",
            headers={"Location": "/login"},
        )
    return user


def require_admin(
    user: Annotated[CurrentUser, Depends(current_user)]
) -> CurrentUser:
    """يفرض صلاحية الإدارة على العمليات المتغيّرة."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="هذه العملية للإدارة فقط"
        )
    return user


async def verify_csrf(
    request: Request, user: Annotated[CurrentUser, Depends(current_user)]
) -> None:
    """تحقق CSRF لكل طلب متغيّر يعتمد على الجلسة (SRS §NFR-003).

    الرمز يُقبل من الحقل ``csrf_token`` في نموذج HTML أو من ترويسة
    ``X-CSRF-Token`` لطلبات HTMX/JSON.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        content_type = request.headers.get("content-type", "")
        if "form" in content_type:
            form = await request.form()
            token = str(form.get("csrf_token", ""))
    if not verify_csrf_token(user.session_id, token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="رمز الحماية غير صالح"
        )


def user_by_username(db: Session, username: str) -> User | None:
    return db.execute(
        select(User).where(User.username == username.strip().lower())
    ).scalar_one_or_none()
