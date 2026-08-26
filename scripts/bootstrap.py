"""تجهيز أول تشغيل: إنشاء حساب الإدارة وملف تعريف المضخة.

    python -m scripts.bootstrap                # يقرأ BOOTSTRAP_ADMIN_* من البيئة
    python -m scripts.bootstrap --username ali --password '...'

الأمر آمن للتكرار: لا ينشئ حسابًا موجودًا، ولا يغيّر كلمة مرور قائمة إلا
بـ``--force``.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.session import session_scope
from app.models import Controller, PumpProfile, User, UserRole
from app.services import audit

logger = get_logger("bootstrap")


def ensure_admin(username: str, password: str, *, force: bool = False) -> str:
    if len(password) < 12:
        raise SystemExit("كلمة مرور الإدارة يجب ألا تقل عن 12 محرفًا.")
    with session_scope() as db:
        existing = db.query(User).filter(User.username == username.lower()).one_or_none()
        if existing is not None:
            if not force:
                return f"الحساب {username} موجود مسبقًا — لم يتغير شيء."
            existing.password_hash = hash_password(password)
            existing.session_epoch += 1  # يُسقط الجلسات القائمة
            existing.is_active = True
            audit.record(db, actor="bootstrap", action="user.password_reset",
                         entity_type="user", entity_id=str(existing.id))
            return f"تم تحديث كلمة مرور {username} وإبطال جلساته."
        user = User(
            username=username.lower(),
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(user)
        db.flush()
        audit.record(db, actor="bootstrap", action="user.created",
                     entity_type="user", entity_id=str(user.id))
        return f"تم إنشاء حساب الإدارة {username}."


def ensure_pump_profiles() -> str:
    """يضيف ملف مضخة افتراضيًا لكل كنترولر لا يملك واحدًا (SRS §4.2)."""
    settings = get_settings()
    created = 0
    with session_scope() as db:
        for controller in db.query(Controller).all():
            if controller.pump_profile is not None:
                continue
            db.add(
                PumpProfile(
                    controller_id=controller.id,
                    brand="PRAKASH",
                    model="PSM5.5-100T",
                    rated_hp=Decimal("5.5"),
                    rated_kw=Decimal(str(settings.pump_rated_kw)),
                    estimated_input_kw=Decimal(str(settings.pump_estimated_input_kw)),
                    voltage_min=Decimal("380"),
                    voltage_max=Decimal("400"),
                    rated_current_a=Decimal("9.8"),
                    frequency_hz=Decimal("50"),
                    rpm=2850,
                    head_min_m=Decimal("60"),
                    head_max_m=Decimal("148"),
                    flow_min_lpm=Decimal(str(settings.default_flow_min_lpm)),
                    flow_default_lpm=Decimal(str(settings.default_flow_lpm)),
                    flow_max_lpm=Decimal(str(settings.default_flow_max_lpm)),
                    well_depth_m=Decimal(str(settings.well_depth_m)),
                    notes="لا تشغّل المضخة بدون ماء. الحماية الكهربائية مسؤولية كهربائي مختص.",
                )
            )
            created += 1
    return f"ملفات مضخة أُنشئت: {created}."


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    settings = get_settings()
    parser = argparse.ArgumentParser(description="تجهيز أول تشغيل")
    parser.add_argument("--username", default=settings.bootstrap_admin_username)
    parser.add_argument("--password", default=settings.bootstrap_admin_password.get_secret_value())
    parser.add_argument("--force", action="store_true", help="إعادة ضبط كلمة مرور حساب قائم")
    parser.add_argument("--skip-pump", action="store_true")
    args = parser.parse_args(argv)

    if not args.password:
        print(
            "لا توجد كلمة مرور: مرّر --password أو اضبط BOOTSTRAP_ADMIN_PASSWORD.",
            file=sys.stderr,
        )
        return 2

    print(ensure_admin(args.username, args.password, force=args.force))
    if not args.skip_pump:
        print(ensure_pump_profiles())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
