"""محرّك قاعدة البيانات والجلسات.

التطبيق والـWorker يشتركان في نفس الإعداد. المحرّك يُنشأ كسولًا حتى تتمكن
الاختبارات من ضبط ``DATABASE_URL`` قبل أول اتصال.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=Session
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """جلسة تُغلق دائمًا، وتُرجَّع عند الخطأ."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """اعتمادية FastAPI."""
    with session_scope() as session:
        yield session


def dispose_engine() -> None:
    """يُستخدم في الاختبارات وعند إغلاق التطبيق."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def database_ready() -> bool:
    """فحص صحة قاعدة البيانات لنقطة ``/health``."""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


ADVISORY_LOCK_COLLECTOR = 0x48495231  # "HIR1" — قفل الجامع الوحيد


def try_advisory_lock(session: Session, key: int = ADVISORY_LOCK_COLLECTOR) -> bool:
    """قفل استشاري على مستوى الجلسة يمنع تشغيل جامعَين معًا (SRS §6.2)."""
    result = session.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
    ).scalar()
    return bool(result)


def release_advisory_lock(session: Session, key: int = ADVISORY_LOCK_COLLECTOR) -> None:
    session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
