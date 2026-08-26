"""كلمات المرور والجلسات وروابط التقارير العامة (SRS §NFR-003).

* كلمات المرور: Argon2id.
* الجلسة: كعكة موقّعة (`itsdangerous`) بخصائص ``Secure/HttpOnly/SameSite``.
* CSRF: رمز مرتبط بالجلسة يُقارن بزمن ثابت.
* الرابط العام: رمز عشوائي 256-bit؛ لا يُخزَّن خامًا بل تُخزَّن بصمته SHA-256.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

__all__ = [
    "hash_password",
    "verify_password",
    "needs_rehash",
    "new_public_token",
    "hash_public_token",
    "verify_public_token",
    "SessionCodec",
    "csrf_token_for",
    "verify_csrf_token",
    "RateLimiter",
]

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,  # Argon2id
)

SESSION_SALT = "hir.session.v1"
CSRF_SALT = "hir.csrf.v1"


# ----------------------------------------------------------------------
# كلمات المرور
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


# ----------------------------------------------------------------------
# روابط التقارير العامة
# ----------------------------------------------------------------------
def new_public_token() -> str:
    """رمز عشوائي 256-bit صالح للاستخدام في مسار URL."""
    return secrets.token_urlsafe(32)


def hash_public_token(token: str) -> str:
    """البصمة المخزَّنة — لا يُحفظ الرمز الخام في قاعدة البيانات."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_public_token(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_public_token(token), expected_hash or "")


# ----------------------------------------------------------------------
# الجلسات و CSRF
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class SessionCodec:
    """توقيع محتوى الجلسة والتحقق منه مع صلاحية زمنية."""

    max_age_seconds: int = 12 * 3600

    def _serializer(self, salt: str) -> URLSafeTimedSerializer:
        secret = get_settings().app_secret_key.get_secret_value()
        return URLSafeTimedSerializer(secret, salt=salt)

    def dumps(self, payload: dict[str, object]) -> str:
        return self._serializer(SESSION_SALT).dumps(payload)

    def loads(self, value: str) -> dict[str, object] | None:
        try:
            data = self._serializer(SESSION_SALT).loads(
                value, max_age=self.max_age_seconds
            )
        except (BadSignature, SignatureExpired):
            return None
        return data if isinstance(data, dict) else None


def csrf_token_for(session_id: str) -> str:
    """رمز CSRF مرتبط بالجلسة — يتغير بتغيرها ولا يصلح لجلسة أخرى."""
    secret = get_settings().app_secret_key.get_secret_value().encode("utf-8")
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(session_id: str, token: str) -> bool:
    return hmac.compare_digest(csrf_token_for(session_id), token or "")


# ----------------------------------------------------------------------
# تحديد المعدل
# ----------------------------------------------------------------------
class RateLimiter:
    """نافذة منزلقة بسيطة في الذاكرة لحماية الدخول والمسارات الحساسة.

    كافية لنشر بنسخة واحدة كما في SRS §26؛ عند التوسع الأفقي تُستبدل
    بمخزن مشترك (Redis) دون تغيير المستدعي.
    """

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}

    def hit(self, key: str, *, now: float | None = None) -> bool:
        """يسجّل محاولة ويعيد ``True`` إن كانت مسموحة."""
        moment = now if now is not None else time.monotonic()
        window = self._events.setdefault(key, [])
        cutoff = moment - self.window_seconds
        window[:] = [event for event in window if event > cutoff]
        if len(window) >= self.max_events:
            return False
        window.append(moment)
        return True

    def reset(self, key: str) -> None:
        self._events.pop(key, None)

    def clear(self) -> None:
        """يمسح كل النوافذ — تستخدمه الاختبارات لعزل الحالات عن بعضها."""
        self._events.clear()
