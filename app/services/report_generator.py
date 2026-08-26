"""بناء التقارير اليومية والشهرية من قاعدة البيانات (SRS §FR-006..FR-009، §22).

التقرير الشهري يُجمَّد: يُحسب مرة، ويُحفظ ``summary_json`` كاملًا، وتُقرأ
الصفحة و PDF و CSV منه جميعًا حتى تتطابق الإجماليات بين الصيغ الثلاث
(AC-008) ولا تتغير أرقام تقرير صادر عند تعديل معايرة لاحقًا.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time import (
    local_day_bounds,
    local_month_bounds,
    month_key,
    to_local,
    utcnow,
)
from app.models import (
    Controller,
    DataGap,
    MonthlyReport,
    ReportStatus,
    Zone,
    ZoneRuntimeEvent,
)
from app.services.calculator import (
    DayAggregate,
    Interval,
    PeriodTotals,
    ZoneAggregate,
    ZoneRun,
    aggregate_days,
    aggregate_zones,
    clip,
    coverage_grade,
    merge_intervals,
    period_totals,
)

logger = get_logger(__name__)

#: تشغيل أقصر من هذا يُعدّ "تشغيلة قصيرة جدًا" في التنبيهات (SRS §22.2).
VERY_SHORT_RUN_SECONDS = 120
SHORT_RUNS_PER_DAY_ALERT = 10
LONG_RUN_RATIO = 1.5
MONTHLY_INCREASE_ALERT = 25.0

DISCLAIMER = (
    "استهلاك المياه الوارد في هذا التقرير تقديري، ومحسوب من مدة التشغيل ومعدل "
    "تدفق مضبوط في النظام. التدفق الفعلي قد يختلف حسب ضغط الشبكة ومنسوب البئر "
    "وارتفاع الضخ وخسائر الأنابيب. كما أن الطاقة قيمة اسمية تقديرية وليست بديلًا "
    "عن قراءة عداد كهرباء."
)


# ----------------------------------------------------------------------
# تحميل البيانات
# ----------------------------------------------------------------------
def _load_runs(
    db: Session,
    controller: Controller,
    period_start: datetime,
    period_end: datetime,
    *,
    now: datetime | None = None,
) -> list[ZoneRun]:
    """أحداث الفترة، مقصوصة على حدودها.

    الحدث المفتوح يُحتسب حتى اللحظة الحالية فقط — لا يُفترض أنه اكتمل.
    """
    moment = now or utcnow()
    rows = db.execute(
        select(ZoneRuntimeEvent, Zone)
        .join(Zone, Zone.id == ZoneRuntimeEvent.zone_id)
        .where(Zone.controller_id == controller.id)
        .where(ZoneRuntimeEvent.started_at < period_end)
        .where(
            or_(
                ZoneRuntimeEvent.ended_at.is_(None),
                ZoneRuntimeEvent.ended_at > period_start,
            )
        )
        .order_by(ZoneRuntimeEvent.started_at)
    ).all()

    runs: list[ZoneRun] = []
    for event, zone in rows:
        ended_at = event.ended_at or min(moment, period_end)
        run = ZoneRun(
            zone_id=str(zone.id),
            zone_number=zone.physical_number,
            zone_name=zone.label,
            started_at=event.started_at,
            ended_at=ended_at,
            flow_lpm=float(event.flow_rate_lpm_snapshot or zone.flow_rate_lpm),
            flow_min_lpm=float(event.flow_min_lpm_snapshot or zone.flow_min_lpm),
            flow_max_lpm=float(event.flow_max_lpm_snapshot or zone.flow_max_lpm),
            confidence=event.confidence.value,
            is_open=event.ended_at is None,
        )
        clipped = clip(run, period_start, period_end)
        if clipped is not None:
            runs.append(clipped)
    return runs


def _load_gaps(
    db: Session,
    controller: Controller,
    period_start: datetime,
    period_end: datetime,
    *,
    now: datetime | None = None,
) -> list[tuple[Interval, str]]:
    moment = now or utcnow()
    rows = db.execute(
        select(DataGap)
        .where(DataGap.controller_id == controller.id)
        .where(DataGap.started_at < period_end)
        .where(or_(DataGap.ended_at.is_(None), DataGap.ended_at > period_start))
        .order_by(DataGap.started_at)
    ).scalars()
    gaps: list[tuple[Interval, str]] = []
    for gap in rows:
        end = gap.ended_at or min(moment, period_end)
        start = max(gap.started_at, period_start)
        end = min(end, period_end)
        if end > start:
            gaps.append((Interval(start, end), gap.reason.value))
    return gaps


def _estimated_input_kw(controller: Controller) -> float:
    profile = controller.pump_profile
    if profile is not None and profile.estimated_input_kw is not None:
        return float(profile.estimated_input_kw)
    return float(get_settings().pump_estimated_input_kw)


# ----------------------------------------------------------------------
# التقرير اليومي
# ----------------------------------------------------------------------
@dataclass
class DailySummary:
    """ملخص يوم واحد — يُحسب عند الطلب ولا يُخزَّن (SRS §FR-006)."""

    day: date
    controller_name: str
    period_start: datetime
    period_end: datetime
    totals: PeriodTotals
    zones: list[ZoneAggregate]
    gap_seconds: float
    gap_reasons: list[str]

    @property
    def event_count(self) -> int:
        return self.totals.event_count

    @property
    def has_gap(self) -> bool:
        return self.gap_seconds > 0


def generate_daily_summary(
    db: Session, controller: Controller, *, day: date, now: datetime | None = None
) -> DailySummary:
    start, end = local_day_bounds(day, controller.timezone)
    runs = _load_runs(db, controller, start, end, now=now)
    gaps = _load_gaps(db, controller, start, end, now=now)
    totals = period_totals(
        runs,
        period_start=start,
        period_end=end,
        gaps=[interval for interval, _ in gaps],
        estimated_input_kw=_estimated_input_kw(controller),
    )
    return DailySummary(
        day=day,
        controller_name=controller.name,
        period_start=start,
        period_end=end,
        totals=totals,
        zones=aggregate_zones(runs, controller.timezone),
        gap_seconds=sum(
            item.seconds for item in merge_intervals([interval for interval, _ in gaps])
        ),
        gap_reasons=sorted({reason for _, reason in gaps}),
    )


# ----------------------------------------------------------------------
# التنبيهات التحليلية
# ----------------------------------------------------------------------
@dataclass
class Alert:
    """تنبيه معلوماتي — لا يتحكم بالمضخة (SRS §22.2)."""

    code: str
    severity: str  # info | warn | bad
    text: str


def _zone_run_medians(
    db: Session, controller: Controller, before: datetime, months: int = 6
) -> dict[str, float]:
    """وسيط مدة التشغيل التاريخية لكل محبس، أساسًا لتنبيه التشغيل الطويل."""
    since = before - timedelta(days=30 * months)
    rows = db.execute(
        select(Zone.id, ZoneRuntimeEvent.runtime_seconds)
        .join(ZoneRuntimeEvent, ZoneRuntimeEvent.zone_id == Zone.id)
        .where(Zone.controller_id == controller.id)
        .where(ZoneRuntimeEvent.started_at >= since)
        .where(ZoneRuntimeEvent.started_at < before)
        .where(ZoneRuntimeEvent.runtime_seconds.isnot(None))
    ).all()
    grouped: dict[str, list[float]] = {}
    for zone_id, seconds in rows:
        if seconds and seconds > 0:
            grouped.setdefault(str(zone_id), []).append(float(seconds))
    return {
        zone_id: statistics.median(values)
        for zone_id, values in grouped.items()
        if len(values) >= 3
    }


def _build_alerts(
    *,
    runs: list[ZoneRun],
    zones: list[ZoneAggregate],
    days: list[DayAggregate],
    totals: PeriodTotals,
    medians: dict[str, float],
    previous_zone_names: set[str],
    previous_water_liters: float | None,
    gaps: list[tuple[Interval, str]],
) -> list[Alert]:
    alerts: list[Alert] = []

    for run in runs:
        median = medians.get(run.zone_id)
        if median and run.interval.seconds > median * LONG_RUN_RATIO:
            alerts.append(
                Alert(
                    "long_run",
                    "warn",
                    f"تشغيل {run.zone_name} في "
                    f"{to_local(run.started_at).strftime('%Y-%m-%d %H:%M')} "
                    f"تجاوز 150% من وسيطه التاريخي.",
                )
            )

    if previous_water_liters and previous_water_liters > 0:
        change = (
            100.0
            * (totals.water_liters_estimate - previous_water_liters)
            / previous_water_liters
        )
        if change > MONTHLY_INCREASE_ALERT:
            alerts.append(
                Alert(
                    "monthly_increase",
                    "warn",
                    f"الاستهلاك التقديري ارتفع {change:.1f}% عن الشهر السابق.",
                )
            )

    active_names = {zone.zone_name for zone in zones if zone.runtime_seconds > 0}
    for name in sorted(previous_zone_names - active_names):
        alerts.append(
            Alert(
                "zone_idle",
                "info",
                f"المحبس {name} لم يعمل هذا الشهر وكان نشطًا في الشهر السابق.",
            )
        )

    short_runs_by_day: dict[date, int] = {}
    for run in runs:
        if run.interval.seconds < VERY_SHORT_RUN_SECONDS:
            day = to_local(run.started_at).date()
            short_runs_by_day[day] = short_runs_by_day.get(day, 0) + 1
    for day, count in sorted(short_runs_by_day.items()):
        if count > SHORT_RUNS_PER_DAY_ALERT:
            alerts.append(
                Alert("short_runs", "warn", f"{count} تشغيلة قصيرة جدًا في يوم {day}.")
            )

    if totals.low_confidence_runs:
        alerts.append(
            Alert(
                "low_confidence",
                "warn",
                f"{totals.low_confidence_runs} حدثًا بثقة منخفضة — أزمنتها تقديرية أكثر من غيرها.",
            )
        )

    gap_intervals = [interval for interval, _ in gaps]
    for run in runs:
        if any(
            gap.start < run.ended_at and run.started_at < gap.end for gap in gap_intervals
        ):
            alerts.append(
                Alert(
                    "run_in_gap",
                    "warn",
                    f"تشغيل {run.zone_name} تداخل مع فجوة بيانات؛ مدته قد تكون ناقصة.",
                )
            )
            break

    # تشغيل متزامن — متوقع أن يعمل محبس واحد في كل مرة على هذا الموقع.
    ordered = sorted(runs, key=lambda item: item.started_at)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.started_at < earlier.ended_at and later.zone_id != earlier.zone_id:
            alerts.append(
                Alert(
                    "concurrent_runs",
                    "info",
                    f"تداخل تشغيل {earlier.zone_name} مع {later.zone_name} — زمن المضخة "
                    "محسوب باتحاد الفترات لا بجمعها.",
                )
            )
            break

    return alerts


# ----------------------------------------------------------------------
# التقرير الشهري
# ----------------------------------------------------------------------
def _zone_payload(zone: ZoneAggregate, total_liters: float) -> dict[str, Any]:
    return {
        "zone_id": zone.zone_id,
        "number": zone.zone_number,
        "name": zone.zone_name,
        "flow_lpm": round(zone.flow_lpm, 2),
        "run_count": zone.run_count,
        "runtime_seconds": round(zone.runtime_seconds),
        "runtime_hours": round(zone.runtime_hours, 3),
        "average_seconds_per_active_day": round(zone.average_seconds_per_active_day),
        "active_days": len(zone.active_days),
        "longest_run_seconds": round(zone.longest_run_seconds),
        "shortest_run_seconds": (
            round(zone.shortest_run_seconds) if zone.shortest_run_seconds else 0
        ),
        "water_liters_estimate": round(zone.water_liters_estimate, 2),
        "water_liters_min": round(zone.water_liters_min, 2),
        "water_liters_max": round(zone.water_liters_max, 2),
        "share_percent": round(zone.share_percent(total_liters), 2),
        "low_confidence_runs": zone.low_confidence_runs,
    }


def build_monthly_payload(
    db: Session,
    controller: Controller,
    *,
    year: int,
    month: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """يحسب محتوى التقرير الشهري كاملًا دون حفظه."""
    settings = get_settings()
    moment = now or utcnow()
    period_start, period_end = local_month_bounds(year, month, controller.timezone)

    runs = _load_runs(db, controller, period_start, period_end, now=moment)
    gaps = _load_gaps(db, controller, period_start, period_end, now=moment)
    input_kw = _estimated_input_kw(controller)

    totals = period_totals(
        runs,
        period_start=period_start,
        period_end=period_end,
        gaps=[interval for interval, _ in gaps],
        estimated_input_kw=input_kw,
    )
    zones = aggregate_zones(runs, controller.timezone)
    days = aggregate_days(runs, period_start, period_end, controller.timezone)

    # المقارنة بالشهر السابق تُحسب من الأحداث مباشرة حتى تعمل حتى لو لم
    # يُولَّد تقرير الشهر السابق أصلًا.
    previous_year, previous_month_number = (year - 1, 12) if month == 1 else (year, month - 1)
    previous_start, previous_end = local_month_bounds(
        previous_year, previous_month_number, controller.timezone
    )
    previous_runs = _load_runs(db, controller, previous_start, previous_end, now=moment)
    previous_totals = (
        period_totals(
            previous_runs,
            period_start=previous_start,
            period_end=previous_end,
            gaps=[
                interval
                for interval, _ in _load_gaps(
                    db, controller, previous_start, previous_end, now=moment
                )
            ],
            estimated_input_kw=input_kw,
        )
        if previous_runs
        else None
    )
    previous_zone_names = {
        zone.zone_name
        for zone in aggregate_zones(previous_runs, controller.timezone)
        if zone.runtime_seconds > 0
    }

    alerts = _build_alerts(
        runs=runs,
        zones=zones,
        days=days,
        totals=totals,
        medians=_zone_run_medians(db, controller, period_start),
        previous_zone_names=previous_zone_names,
        previous_water_liters=previous_totals.water_liters_estimate if previous_totals else None,
        gaps=gaps,
    )

    grade_text, grade_tone = coverage_grade(totals.coverage_percent)
    top_zone = max(zones, key=lambda z: z.water_liters_estimate, default=None)
    top_day = max(days, key=lambda d: d.water_liters_estimate, default=None)
    active_days = [day for day in days if day.runtime_seconds > 0]

    def _change(current: float, previous: float | None) -> float | None:
        if previous is None or previous <= 0:
            return None
        return round(100.0 * (current - previous) / previous, 2)

    return {
        "schema_version": 1,
        "controller": {
            "name": controller.name,
            "hydrawise_controller_id": controller.hydrawise_controller_id,
            "serial_masked": controller.masked_serial,
        },
        "month": month_key(year, month),
        "timezone": controller.timezone,
        "generated_at": moment.isoformat(),
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "metrics": {
            "pump_runtime_seconds": round(totals.pump_runtime_seconds),
            "zone_runtime_seconds": round(totals.zone_runtime_seconds),
            "water_estimate_liters": round(totals.water_liters_estimate, 2),
            "water_min_liters": round(totals.water_liters_min, 2),
            "water_max_liters": round(totals.water_liters_max, 2),
            "energy_estimate_kwh": round(totals.energy_kwh, 3),
            "event_count": totals.event_count,
            "coverage_percent": round(totals.coverage_percent, 2),
            "low_confidence_runs": totals.low_confidence_runs,
            "average_runtime_seconds_per_active_day": (
                round(totals.zone_runtime_seconds / len(active_days)) if active_days else 0
            ),
        },
        "coverage": {
            "percent": round(totals.coverage_percent, 2),
            "grade": grade_text,
            "tone": grade_tone,
            "gap_seconds": round(
                sum(i.seconds for i in merge_intervals([i for i, _ in gaps]))
            ),
            "reasons": sorted({reason for _, reason in gaps}),
        },
        "zones": [_zone_payload(zone, totals.water_liters_estimate) for zone in zones],
        "daily": [
            {
                "day": day.day.isoformat(),
                "runtime_seconds": round(day.runtime_seconds),
                "pump_seconds": round(day.pump_seconds),
                "water_liters": round(day.water_liters_estimate, 2),
                "run_count": day.run_count,
            }
            for day in days
        ],
        "highlights": {
            "top_zone_by_water": top_zone.zone_name if top_zone else None,
            "top_zone_liters": round(top_zone.water_liters_estimate, 2) if top_zone else 0,
            "top_day": top_day.day.isoformat() if top_day else None,
            "top_day_liters": round(top_day.water_liters_estimate, 2) if top_day else 0,
            "active_days": len(active_days),
        },
        "comparison": (
            {
                "month": month_key(previous_year, previous_month_number),
                "water_estimate_liters": round(previous_totals.water_liters_estimate, 2),
                "pump_runtime_seconds": round(previous_totals.pump_runtime_seconds),
                "energy_estimate_kwh": round(previous_totals.energy_kwh, 3),
                "event_count": previous_totals.event_count,
                "water_change_percent": _change(
                    totals.water_liters_estimate, previous_totals.water_liters_estimate
                ),
                "pump_change_percent": _change(
                    totals.pump_runtime_seconds, previous_totals.pump_runtime_seconds
                ),
            }
            if previous_totals
            else None
        ),
        "alerts": [
            {"code": alert.code, "severity": alert.severity, "text": alert.text}
            for alert in alerts
        ],
        "methodology": {
            "default_flow_lpm": settings.default_flow_lpm,
            "flow_min_lpm": settings.default_flow_min_lpm,
            "flow_max_lpm": settings.default_flow_max_lpm,
            "pump_input_kw": input_kw,
            "water_is_estimated": True,
            "energy_is_estimated": True,
            # قائمة الموقع لا قائمة الشهر: محبس لم يعمل هذا الشهر يبقى غير
            # معاير، وإخفاؤه يوحي بأن كل المحابس معايرة.
            "uncalibrated_zones": _uncalibrated_zone_names(db, controller),
        },
        "disclaimer": DISCLAIMER,
        "has_data": bool(runs),
    }


def _uncalibrated_zone_names(db: Session, controller: Controller) -> list[str]:
    """أسماء المحابس النشطة التي ما زالت على التدفق الافتراضي."""
    zones = db.execute(
        select(Zone)
        .where(Zone.controller_id == controller.id)
        .where(Zone.is_active.is_(True))
        .order_by(Zone.physical_number)
    ).scalars()
    return [zone.label for zone in zones if not zone.is_calibrated]


def generate_monthly_report(
    db: Session,
    controller: Controller,
    *,
    year: int,
    month: int,
    now: datetime | None = None,
    status: ReportStatus = ReportStatus.FINAL,
) -> MonthlyReport:
    """يولّد التقرير الشهري ويستبدل أي نسخة سابقة لنفس الشهر (SRS §9.8)."""
    payload = build_monthly_payload(db, controller, year=year, month=month, now=now)
    period_start = datetime.fromisoformat(payload["period"]["start"])
    period_end = datetime.fromisoformat(payload["period"]["end"])
    metrics = payload["metrics"]

    report = db.execute(
        select(MonthlyReport)
        .where(MonthlyReport.controller_id == controller.id)
        .where(MonthlyReport.month == date(year, month, 1))
    ).scalar_one_or_none()
    if report is None:
        report = MonthlyReport(controller_id=controller.id, month=date(year, month, 1))
        db.add(report)

    report.status = status
    report.generated_at = now or utcnow()
    report.period_start = period_start
    report.period_end = period_end
    report.total_zone_runtime_seconds = int(metrics["zone_runtime_seconds"])
    report.pump_union_runtime_seconds = int(metrics["pump_runtime_seconds"])
    report.total_water_liters_estimate = Decimal(str(metrics["water_estimate_liters"]))
    report.total_water_liters_min = Decimal(str(metrics["water_min_liters"]))
    report.total_water_liters_max = Decimal(str(metrics["water_max_liters"]))
    report.energy_kwh_estimate = Decimal(str(metrics["energy_estimate_kwh"]))
    report.event_count = int(metrics["event_count"])
    report.data_coverage_percent = Decimal(str(metrics["coverage_percent"]))
    report.summary_json = payload
    db.flush()

    logger.info(
        "report.generated",
        extra={
            "controller": controller.name,
            "month": payload["month"],
            "events": report.event_count,
            "coverage": float(report.data_coverage_percent),
        },
    )
    return report
