"""روابط التقارير العامة المؤقتة (SRS §NFR-003، §12.1).

الرابط يحمل رمزًا عشوائيًا 256-bit؛ قاعدة البيانات تحفظ بصمته فقط، فلا
يستطيع من يقرأ قاعدة البيانات أن يولّد الرابط. للرمز تاريخ انتهاء، ويمكن
إبطاله فورًا بمسح البصمة.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import hash_public_token, new_public_token
from app.core.time import utcnow
from app.models import MonthlyReport

__all__ = ["issue_share_token", "revoke_share_token", "resolve_share_token", "public_url"]


def public_url(token: str) -> str:
    return f"{get_settings().public_base_url}/r/{token}"


def issue_share_token(report: MonthlyReport, *, ttl_days: int | None = None) -> str:
    """يصدر رمزًا جديدًا (ويُبطل السابق ضمنًا) ويعيد الرمز الخام مرة واحدة."""
    settings = get_settings()
    token = new_public_token()
    report.public_token_hash = hash_public_token(token)
    report.public_token_expires_at = utcnow() + timedelta(
        days=ttl_days or settings.report_public_link_ttl_days
    )
    return token


def revoke_share_token(report: MonthlyReport) -> None:
    report.public_token_hash = None
    report.public_token_expires_at = None


def resolve_share_token(db: Session, token: str) -> MonthlyReport | None:
    """يعيد التقرير المرتبط برمز صالح غير منتهٍ، أو ``None``."""
    if not token or len(token) < 20:
        return None
    digest = hash_public_token(token)
    report = db.execute(
        select(MonthlyReport).where(MonthlyReport.public_token_hash == digest)
    ).scalar_one_or_none()
    if report is None:
        return None
    if report.public_token_expires_at and report.public_token_expires_at <= utcnow():
        return None
    return report
