"""سجل التدقيق — كل تغيير إداري وكل محاولة دخول (SRS §8.9، §FR-013)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger, redact
from app.core.time import utcnow
from app.models import AuditLog

logger = get_logger(__name__)


def record(
    db: Session,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    client_ip: str | None = None,
) -> AuditLog:
    """يضيف سطرًا للسجل. لا يُنفَّذ commit هنا — يتبع معاملة المستدعي."""
    entry = AuditLog(
        occurred_at=utcnow(),
        actor=actor[:80],
        action=action[:80],
        entity_type=entity_type,
        entity_id=str(entity_id)[:80] if entity_id is not None else None,
        reason=redact(reason) if reason else None,
        before_json=before,
        after_json=after,
        client_ip=client_ip,
    )
    db.add(entry)
    logger.info("audit", extra={"action": action, "actor": actor, "entity": entity_type})
    return entry


def recent(db: Session, limit: int = 100) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
        ).scalars()
    )
