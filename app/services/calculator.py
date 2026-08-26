"""حسابات المياه والطاقة وزمن المضخة والتغطية (SRS §10).

كل الحساب هنا خالص: يدخل عليه أحداث تشغيل وفجوات، ويخرج منه أرقام. لا
قاعدة بيانات ولا وقت حاضر ضمني — وهو ما يجعل اختبارات القبول (AC-005،
AC-006، AC-007) قابلة للتنفيذ بلا بنية تحتية.

المعادلات كما وردت في الوثيقة:

``water_liters = runtime_minutes × flow_rate_lpm``
``pump_runtime = duration(union(all zone intervals))``
``energy_kwh = pump_runtime_hours × estimated_input_kw``
``coverage = (period - gaps) / period``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.core.time import to_local

__all__ = [
    "Interval",
    "ZoneRun",
    "ZoneAggregate",
    "DayAggregate",
    "PeriodTotals",
    "clip",
    "merge_intervals",
    "union_seconds",
    "water_liters",
    "energy_kwh",
    "coverage_percent",
    "aggregate_zones",
    "aggregate_days",
    "period_totals",
    "coverage_grade",
]


@dataclass(frozen=True)
class Interval:
    """فترة زمنية نصف مفتوحة ``[start, end)``."""

    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True)
class ZoneRun:
    """حدث تشغيل واحد بالشكل الذي يحتاجه الحساب.

    ``flow_*_lpm`` مجمّدة وقت إغلاق الحدث، فتغيير معايرة المحبس لاحقًا لا
    يغيّر تقارير صدرت من قبل (SRS §FR-012).
    """

    zone_id: str
    zone_number: int | None
    zone_name: str
    started_at: datetime
    ended_at: datetime
    flow_lpm: float
    flow_min_lpm: float
    flow_max_lpm: float
    confidence: str = "medium"
    is_open: bool = False

    @property
    def interval(self) -> Interval:
        return Interval(self.started_at, self.ended_at)


def clip(run: ZoneRun, period_start: datetime, period_end: datetime) -> ZoneRun | None:
    """يقصّ حدثًا على حدود الفترة.

    حدث يعبر منتصف ليل آخر الشهر يُحتسب في الشهرين بنسبة ما وقع في كل
    منهما، دون تغيير السجل الأصلي (SRS §10.7، AC-007).
    """
    start = max(run.started_at, period_start)
    end = min(run.ended_at, period_end)
    if end <= start:
        return None
    if start == run.started_at and end == run.ended_at:
        return run
    return ZoneRun(
        zone_id=run.zone_id,
        zone_number=run.zone_number,
        zone_name=run.zone_name,
        started_at=start,
        ended_at=end,
        flow_lpm=run.flow_lpm,
        flow_min_lpm=run.flow_min_lpm,
        flow_max_lpm=run.flow_max_lpm,
        confidence=run.confidence,
        is_open=run.is_open,
    )


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """يدمج الفترات المتداخلة أو المتلامسة."""
    ordered = sorted(
        (item for item in intervals if item.end > item.start), key=lambda i: i.start
    )
    merged: list[Interval] = []
    for item in ordered:
        if merged and item.start <= merged[-1].end:
            if item.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, item.end)
        else:
            merged.append(item)
    return merged


def union_seconds(intervals: list[Interval]) -> float:
    """طول اتحاد الفترات — أساس زمن المضخة (SRS §10.4، AC-006)."""
    return sum(item.seconds for item in merge_intervals(intervals))


def water_liters(runtime_seconds: float, flow_lpm: float) -> float:
    """``runtime_minutes × flow_lpm`` (SRS §10.2)."""
    return (runtime_seconds / 60.0) * flow_lpm


def energy_kwh(pump_runtime_seconds: float, estimated_input_kw: float) -> float:
    """``pump_runtime_hours × estimated_input_kw`` (SRS §10.5)."""
    return (pump_runtime_seconds / 3600.0) * estimated_input_kw


def coverage_percent(
    period_start: datetime, period_end: datetime, gaps: list[Interval]
) -> float:
    """نسبة الفترة التي كان الجمع فيها يعمل (SRS §10.6)."""
    period = Interval(period_start, period_end).seconds
    if period <= 0:
        return 0.0
    clipped = [
        Interval(max(gap.start, period_start), min(gap.end, period_end))
        for gap in gaps
        if gap.end > period_start and gap.start < period_end
    ]
    missing = union_seconds(clipped)
    return max(0.0, 100.0 * (period - missing) / period)


def coverage_grade(percent: float) -> tuple[str, str]:
    """تصنيف جودة التغطية ونبرته اللونية (SRS §10.6)."""
    if percent >= 99:
        return "ممتازة", "good"
    if percent >= 95:
        return "جيدة", "info"
    if percent >= 85:
        return "تحتاج مراجعة", "warn"
    return "غير مكتملة", "bad"


@dataclass
class ZoneAggregate:
    """ملخص محبس واحد داخل الفترة (SRS §FR-008)."""

    zone_id: str
    zone_number: int | None
    zone_name: str
    flow_lpm: float
    run_count: int = 0
    runtime_seconds: float = 0.0
    longest_run_seconds: float = 0.0
    shortest_run_seconds: float | None = None
    water_liters_estimate: float = 0.0
    water_liters_min: float = 0.0
    water_liters_max: float = 0.0
    active_days: set[date] = field(default_factory=set)
    low_confidence_runs: int = 0

    @property
    def runtime_hours(self) -> float:
        return self.runtime_seconds / 3600.0

    @property
    def average_seconds_per_active_day(self) -> float:
        return self.runtime_seconds / len(self.active_days) if self.active_days else 0.0

    @property
    def water_m3_estimate(self) -> float:
        return self.water_liters_estimate / 1000.0

    def share_percent(self, total_liters: float) -> float:
        return 0.0 if total_liters <= 0 else 100.0 * self.water_liters_estimate / total_liters


@dataclass
class DayAggregate:
    """ملخص يوم واحد بتوقيت الموقع."""

    day: date
    runtime_seconds: float = 0.0
    water_liters_estimate: float = 0.0
    run_count: int = 0
    pump_seconds: float = 0.0

    @property
    def water_m3(self) -> float:
        return self.water_liters_estimate / 1000.0


@dataclass
class PeriodTotals:
    """إجماليات الفترة كاملة."""

    zone_runtime_seconds: float = 0.0
    pump_runtime_seconds: float = 0.0
    water_liters_estimate: float = 0.0
    water_liters_min: float = 0.0
    water_liters_max: float = 0.0
    energy_kwh: float = 0.0
    event_count: int = 0
    coverage_percent: float = 100.0
    low_confidence_runs: int = 0

    @property
    def water_m3_estimate(self) -> float:
        return self.water_liters_estimate / 1000.0

    @property
    def pump_runtime_hours(self) -> float:
        return self.pump_runtime_seconds / 3600.0


def _split_by_local_day(
    run: ZoneRun, timezone_name: str | None = None
) -> list[tuple[date, ZoneRun]]:
    """يقسم حدثًا يعبر منتصف الليل على أيامه المحلية."""
    pieces: list[tuple[date, ZoneRun]] = []
    cursor = run.started_at
    while cursor < run.ended_at:
        local = to_local(cursor, timezone_name)
        day_end_local = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        boundary = day_end_local.astimezone(run.ended_at.tzinfo or day_end_local.tzinfo)
        end = min(run.ended_at, boundary)
        piece = clip(run, cursor, end)
        if piece is not None:
            pieces.append((local.date(), piece))
        cursor = end
    return pieces


def aggregate_zones(
    runs: list[ZoneRun], timezone_name: str | None = None
) -> list[ZoneAggregate]:
    """يجمّع الأحداث حسب المحبس."""
    buckets: dict[str, ZoneAggregate] = {}
    for run in runs:
        seconds = run.interval.seconds
        if seconds <= 0:
            continue
        aggregate = buckets.get(run.zone_id)
        if aggregate is None:
            aggregate = ZoneAggregate(
                zone_id=run.zone_id,
                zone_number=run.zone_number,
                zone_name=run.zone_name,
                flow_lpm=run.flow_lpm,
            )
            buckets[run.zone_id] = aggregate
        aggregate.run_count += 1
        aggregate.runtime_seconds += seconds
        aggregate.longest_run_seconds = max(aggregate.longest_run_seconds, seconds)
        aggregate.shortest_run_seconds = (
            seconds
            if aggregate.shortest_run_seconds is None
            else min(aggregate.shortest_run_seconds, seconds)
        )
        aggregate.water_liters_estimate += water_liters(seconds, run.flow_lpm)
        aggregate.water_liters_min += water_liters(seconds, run.flow_min_lpm)
        aggregate.water_liters_max += water_liters(seconds, run.flow_max_lpm)
        aggregate.active_days.add(to_local(run.started_at, timezone_name).date())
        if run.confidence == "low":
            aggregate.low_confidence_runs += 1

    return sorted(
        buckets.values(),
        key=lambda item: (item.zone_number is None, item.zone_number or 0, item.zone_name),
    )


def aggregate_days(
    runs: list[ZoneRun],
    period_start: datetime,
    period_end: datetime,
    timezone_name: str | None = None,
) -> list[DayAggregate]:
    """يجمّع الأحداث حسب اليوم المحلي، مع أيام صفرية للأيام الخالية."""
    per_day: dict[date, DayAggregate] = {}
    intervals_by_day: dict[date, list[Interval]] = {}

    cursor = to_local(period_start, timezone_name).date()
    last = to_local(period_end - timedelta(seconds=1), timezone_name).date()
    while cursor <= last:
        per_day[cursor] = DayAggregate(day=cursor)
        intervals_by_day[cursor] = []
        cursor += timedelta(days=1)

    for run in runs:
        for day, piece in _split_by_local_day(run, timezone_name):
            bucket = per_day.setdefault(day, DayAggregate(day=day))
            bucket.runtime_seconds += piece.interval.seconds
            bucket.water_liters_estimate += water_liters(piece.interval.seconds, piece.flow_lpm)
            intervals_by_day.setdefault(day, []).append(piece.interval)
        # عدد التشغيلات يُنسب لليوم الذي بدأ فيه الحدث.
        start_day = to_local(run.started_at, timezone_name).date()
        if start_day in per_day:
            per_day[start_day].run_count += 1

    for day, intervals in intervals_by_day.items():
        per_day[day].pump_seconds = union_seconds(intervals)

    return [per_day[day] for day in sorted(per_day)]


def period_totals(
    runs: list[ZoneRun],
    *,
    period_start: datetime,
    period_end: datetime,
    gaps: list[Interval],
    estimated_input_kw: float,
) -> PeriodTotals:
    """يجمع كل شيء في إجماليات فترة واحدة."""
    zone_seconds = sum(run.interval.seconds for run in runs)
    pump_seconds = union_seconds([run.interval for run in runs])
    totals = PeriodTotals(
        zone_runtime_seconds=zone_seconds,
        pump_runtime_seconds=pump_seconds,
        water_liters_estimate=sum(water_liters(r.interval.seconds, r.flow_lpm) for r in runs),
        water_liters_min=sum(water_liters(r.interval.seconds, r.flow_min_lpm) for r in runs),
        water_liters_max=sum(water_liters(r.interval.seconds, r.flow_max_lpm) for r in runs),
        energy_kwh=energy_kwh(pump_seconds, estimated_input_kw),
        event_count=len(runs),
        coverage_percent=coverage_percent(period_start, period_end, gaps),
        low_confidence_runs=sum(1 for run in runs if run.confidence == "low"),
    )
    return totals
