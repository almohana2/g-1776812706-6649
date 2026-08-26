"""تهيئة الاختبارات: قاعدة بيانات حقيقية، ترحيلات حقيقية، وخادم Hydrawise وهمي.

الاختبارات تعمل على PostgreSQL فعلي لا على بديل في الذاكرة، لأن نصف
الضمانات هنا من قاعدة البيانات نفسها: الفهرس الجزئي الفريد الذي يمنع
حدثين مفتوحين، وأنواع ENUM، و``TIMESTAMPTZ``. اختبارها على SQLite يعني
اختبار شيء آخر.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

# يجب ضبط البيئة قبل أول استيراد لوحدات التطبيق (الإعدادات تُقرأ عند الاستيراد).
DEFAULT_ADMIN_DB = "postgresql+psycopg://hydrawise:hydrawise@127.0.0.1:5432/postgres"
TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "hydrawise_test")
TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+psycopg://hydrawise:hydrawise@127.0.0.1:5432/{TEST_DB_NAME}",
)

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-used-in-production-000000")
os.environ.setdefault("REPORT_TIMEZONE", "Asia/Muscat")
os.environ.setdefault("PUBLIC_BASE_URL", "https://reports.test")
os.environ.setdefault("HYDRAWISE_API_KEY", "test-api-key-abcdef")
os.environ.setdefault("HYDRAWISE_API_BASE", "https://api.hydrawise.test/api/v1/")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.pop("OPENWA_ENABLED", None)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import reset_settings_cache  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.models import (  # noqa: E402
    Controller,
    PumpProfile,
    User,
    UserRole,
    Zone,
)

ADMIN_PASSWORD = "test-admin-password-1234"


def _ensure_database() -> None:
    """ينشئ قاعدة الاختبار إن لم تكن موجودة."""
    admin_url = os.environ.get("TEST_ADMIN_DATABASE_URL", DEFAULT_ADMIN_DB)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": TEST_DB_NAME},
        ).first()
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    """قاعدة اختبار مهيّأة بالترحيلات نفسها التي يستعملها الإنتاج."""
    _ensure_database()
    reset_settings_cache()
    db_session.dispose_engine()

    # قاعدة فارغة تمامًا في كل جلسة اختبار: هكذا تُختبر الترحيلات كما
    # ستُنفَّذ في الإنتاج أول مرة (SRS §30).
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(config, "head")
    yield
    db_session.dispose_engine()


@pytest.fixture()
def db(database: None) -> Iterator[Session]:
    """جلسة نظيفة لكل اختبار — تُفرَّغ الجداول قبل البدء."""
    engine = db_session.get_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE zone_runtime_events, poll_samples, data_gaps, "
                "notification_deliveries, monthly_reports, zones, pump_profiles, "
                "controllers, audit_logs, users RESTART IDENTITY CASCADE"
            )
        )
    session = db_session.get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def admin_user(db: Session) -> User:
    user = User(
        username="admin",
        password_hash=hash_password(ADMIN_PASSWORD),
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def viewer_user(db: Session) -> User:
    user = User(
        username="viewer",
        password_hash=hash_password(ADMIN_PASSWORD),
        role=UserRole.VIEWER,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def controller(db: Session) -> Controller:
    controller = Controller(
        hydrawise_controller_id=4242,
        customer_id=1337,
        name="ALMOHANA",
        serial_number="000000000ABC",
        timezone="Asia/Muscat",
    )
    db.add(controller)
    db.flush()
    db.add(
        PumpProfile(
            controller_id=controller.id,
            brand="PRAKASH",
            model="PSM5.5-100T",
            estimated_input_kw=4,
        )
    )
    db.commit()
    return controller


@pytest.fixture()
def zones(db: Session, controller: Controller) -> list[Zone]:
    created = []
    for number, name, flow in ((1, "المسطح الأمامي", 140), (2, "النخيل", 140)):
        zone = Zone(
            controller_id=controller.id,
            hydrawise_relay_id=100000 + number,
            physical_number=number,
            name=name,
            flow_rate_lpm=flow,
            flow_min_lpm=80,
            flow_max_lpm=200,
        )
        db.add(zone)
        created.append(zone)
    db.commit()
    return created


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """محدّد المحاولات يعيش في ذاكرة العملية؛ بدون مسحه يرث اختبارٌ حظرَ
    اختبار سبقه فيفشل لسبب لا علاقة له به."""
    from app.api.auth import _login_limiter

    _login_limiter.clear()
    yield
    _login_limiter.clear()


@pytest.fixture()
def client(db: Session):
    """عميل HTTP للتطبيق يشارك جلسة الاختبار نفسها."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_client(client, admin_user: User):
    response = client.post(
        "/login", data={"username": "admin", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 303, response.text
    return client


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())
