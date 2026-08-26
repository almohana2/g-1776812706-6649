"""عملية الـWorker: الجمع الدوري والجدولة (SRS §6.2، §17).

نسخة واحدة فقط تجمع في أي لحظة، ويضمن ذلك قفل PostgreSQL الاستشاري: أي
نسخة إضافية تبدأ ثم تنسحب بهدوء بدل مضاعفة الطلبات على Hydrawise.

الجدولة الزمنية (اليومي والشهري والتنظيف) تعمل بتوقيت الموقع، بينما تُخزَّن
كل الأوقات في قاعدة البيانات بـUTC.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.time import previous_month, to_local, utcnow
from app.db.session import (
    dispose_engine,
    get_session_factory,
    release_advisory_lock,
    session_scope,
    try_advisory_lock,
)
from app.models import Controller
from app.services.collector import Collector
from app.services.hydrawise_client import HydrawiseClient
from app.services.report_generator import (
    generate_daily_summary,
    generate_monthly_report,
)

logger = get_logger("worker")


class CollectorWorker:
    """يدير حلقة الاستطلاع لكل كنترولر مع احترام ``nextpoll``."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.collector = Collector(HydrawiseClient.from_settings())
        self._stop = asyncio.Event()
        self._halted_reason: str | None = None

    # ------------------------------------------------------------------
    async def bootstrap(self) -> list[int]:
        """اكتشاف أولي + توثيق أي فترة توقف سابقة للجامع."""
        with session_scope() as db:
            controllers = await self.collector.sync_controllers(db)
            for controller in controllers:
                self.collector.record_worker_downtime(db, controller)
            self.collector.close_stale_events(db)
            return [controller.hydrawise_controller_id for controller in controllers]

    async def poll_loop(self) -> None:
        """يستطلع كل كنترولر إلى أن يُطلب الإيقاف."""
        while not self._stop.is_set():
            delay = self.settings.hydrawise_default_poll_seconds
            try:
                delay = await self._poll_all()
            except Exception:  # لا شيء يُسقط الحلقة
                logger.exception("worker.poll_cycle_failed")
            if self._halted_reason:
                logger.error("worker.halted", extra={"reason": self._halted_reason})
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _poll_all(self) -> int:
        delays: list[int] = []
        with session_scope() as db:
            controllers = list(
                db.execute(select(Controller).where(Controller.is_active.is_(True))).scalars()
            )
            if not controllers:
                logger.warning("worker.no_controllers")
                return self.settings.hydrawise_default_poll_seconds
            for controller in controllers:
                outcome = await self.collector.poll(db, controller)
                delays.append(outcome.next_poll_seconds)
                if outcome.fatal:
                    # مفتاح غير صالح: التكرار السريع لا يصلحه ويستنزف الحد.
                    self._halted_reason = outcome.error_message or "مفتاح API مرفوض"
                logger.info(
                    "worker.polled",
                    extra={
                        "controller": controller.name,
                        "ok": outcome.ok,
                        "started": outcome.started_events,
                        "closed": outcome.closed_events,
                        "next_poll_seconds": outcome.next_poll_seconds,
                    },
                )
        return max(delays) if delays else self.settings.hydrawise_default_poll_seconds

    def request_stop(self) -> None:
        self._stop.set()


# ----------------------------------------------------------------------
# مهام مجدولة
# ----------------------------------------------------------------------
async def job_sync_controllers(worker: CollectorWorker) -> None:
    """مزامنة يومية للأسماء — الاستطلاع هو من يكتشف المحابس (SRS §FR-002)."""
    with session_scope() as db:
        controllers = await worker.collector.sync_controllers(db)
        logger.info("worker.controllers_synced", extra={"count": len(controllers)})


def job_close_stale(worker: CollectorWorker) -> None:
    with session_scope() as db:
        closed = worker.collector.close_stale_events(db)
    if closed:
        logger.info("worker.stale_closed", extra={"count": closed})


def job_purge_samples(worker: CollectorWorker) -> None:
    with session_scope() as db:
        worker.collector.purge_old_samples(db)


def job_daily_report() -> None:
    """ملخص اليوم السابق، بحدود يوم الموقع لا يوم UTC (SRS §FR-006)."""
    settings = get_settings()
    yesterday = to_local(utcnow()).date() - timedelta(days=1)
    with session_scope() as db:
        for controller in db.execute(select(Controller)).scalars():
            summary = generate_daily_summary(db, controller, day=yesterday)
            logger.info(
                "worker.daily_report",
                extra={
                    "controller": controller.name,
                    "day": str(summary.day),
                    "events": summary.event_count,
                    "tz": settings.report_timezone,
                },
            )


def job_monthly_report() -> None:
    year, month = previous_month()
    with session_scope() as db:
        for controller in db.execute(select(Controller)).scalars():
            report = generate_monthly_report(db, controller, year=year, month=month)
            logger.info(
                "worker.monthly_report",
                extra={"controller": controller.name, "month": report.month_key},
            )
    _send_monthly(year, month)


def _send_monthly(year: int, month: int) -> None:
    """يرسل تقرير الشهر عبر OpenWA بعد نجاح التوليد (SRS §FR-011)."""
    settings = get_settings()
    if not settings.openwa_configured:
        logger.info("worker.openwa_disabled")
        return
    from app.services.openwa_client import send_monthly_report

    with session_scope() as db:
        for controller in db.execute(select(Controller)).scalars():
            result = send_monthly_report(db, controller, year=year, month=month)
            logger.info(
                "worker.monthly_sent",
                extra={"controller": controller.name, "status": result.status.value},
            )


# ----------------------------------------------------------------------
async def run() -> int:
    configure_logging()
    settings = get_settings()
    logger.info("worker.start", extra={"tz": settings.report_timezone})

    if not settings.hydrawise_configured:
        logger.error("worker.no_api_key")
        return 2

    lock_session = get_session_factory()()
    if not try_advisory_lock(lock_session):
        logger.warning("worker.already_running")
        lock_session.close()
        return 0

    worker = CollectorWorker()
    scheduler = AsyncIOScheduler(timezone=settings.report_timezone)
    scheduler.add_job(
        job_sync_controllers, CronTrigger.from_crontab("0 3 * * *"), args=[worker],
        id="sync-controllers",
    )
    scheduler.add_job(
        job_close_stale, "interval", minutes=10, args=[worker], id="close-stale"
    )
    scheduler.add_job(
        job_purge_samples, CronTrigger.from_crontab("0 4 * * *"), args=[worker],
        id="purge-samples",
    )
    scheduler.add_job(
        job_daily_report, CronTrigger.from_crontab(settings.daily_report_cron),
        id="daily-report",
    )
    scheduler.add_job(
        job_monthly_report, CronTrigger.from_crontab(settings.monthly_report_cron),
        id="monthly-report",
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.bootstrap()
        scheduler.start()
        await worker.poll_loop()
    finally:
        scheduler.shutdown(wait=False)
        release_advisory_lock(lock_session)
        lock_session.close()
        dispose_engine()
        logger.info("worker.stopped")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
