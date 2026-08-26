"""معادلات المياه والطاقة والمضخة والتغطية (SRS §10، AC-005..AC-007)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.calculator import (
    Interval,
    ZoneRun,
    aggregate_days,
    aggregate_zones,
    clip,
    coverage_grade,
    coverage_percent,
    energy_kwh,
    merge_intervals,
    period_totals,
    union_seconds,
    water_liters,
)

UTC = UTC


def run(
    zone_id: str = "z1",
    number: int = 1,
    start: datetime | None = None,
    minutes: int = 60,
    flow: float = 140.0,
    confidence: str = "high",
) -> ZoneRun:
    start = start or datetime(2026, 7, 5, 6, tzinfo=UTC)
    return ZoneRun(
        zone_id=zone_id,
        zone_number=number,
        zone_name=f"محبس {number}",
        started_at=start,
        ended_at=start + timedelta(minutes=minutes),
        flow_lpm=flow,
        flow_min_lpm=80.0,
        flow_max_lpm=200.0,
        confidence=confidence,
    )


class TestWater:
    def test_ac_005_sixty_minutes_at_140_lpm_is_8400_litres(self):
        assert water_liters(3600, 140.0) == pytest.approx(8400.0)
        assert water_liters(3600, 140.0) / 1000 == pytest.approx(8.4)

    def test_range_uses_the_plate_bounds(self):
        assert water_liters(3600, 80.0) == pytest.approx(4800.0)
        assert water_liters(3600, 200.0) == pytest.approx(12000.0)

    def test_zero_runtime_is_zero_water(self):
        assert water_liters(0, 140.0) == 0


class TestPumpUnion:
    def test_ac_006_two_zones_together_do_not_double_the_pump(self):
        start = datetime(2026, 7, 5, 10, tzinfo=UTC)
        a = run("a", 1, start, minutes=30)
        b = run("b", 2, start, minutes=30)
        assert (a.interval.seconds + b.interval.seconds) / 60 == 60
        assert union_seconds([a.interval, b.interval]) / 60 == 30

    def test_sequential_runs_add_up(self):
        start = datetime(2026, 7, 5, 10, tzinfo=UTC)
        a = run("a", 1, start, minutes=30)
        b = run("b", 2, start + timedelta(minutes=30), minutes=30)
        assert union_seconds([a.interval, b.interval]) / 60 == 60

    def test_partial_overlap_counts_once(self):
        start = datetime(2026, 7, 5, 10, tzinfo=UTC)
        a = run("a", 1, start, minutes=30)
        b = run("b", 2, start + timedelta(minutes=20), minutes=30)
        assert union_seconds([a.interval, b.interval]) / 60 == 50

    def test_merge_is_stable_for_touching_intervals(self):
        start = datetime(2026, 7, 5, 10, tzinfo=UTC)
        merged = merge_intervals(
            [
                Interval(start, start + timedelta(minutes=10)),
                Interval(start + timedelta(minutes=10), start + timedelta(minutes=20)),
            ]
        )
        assert len(merged) == 1
        assert merged[0].seconds == 1200


class TestClipping:
    def test_ac_007_a_run_across_the_month_boundary_splits_evenly(self):
        crossing = ZoneRun(
            zone_id="a",
            zone_number=1,
            zone_name="أ",
            started_at=datetime(2026, 7, 31, 23, 50, tzinfo=UTC),
            ended_at=datetime(2026, 8, 1, 0, 10, tzinfo=UTC),
            flow_lpm=140.0,
            flow_min_lpm=80.0,
            flow_max_lpm=200.0,
        )
        july = clip(crossing, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
        august = clip(crossing, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
        assert july.interval.seconds / 60 == 10
        assert august.interval.seconds / 60 == 10

    def test_a_run_entirely_outside_the_period_is_dropped(self):
        outside = run(start=datetime(2026, 6, 1, tzinfo=UTC))
        july_start = datetime(2026, 7, 1, tzinfo=UTC)
        august_start = datetime(2026, 8, 1, tzinfo=UTC)
        assert clip(outside, july_start, august_start) is None

    def test_clipping_preserves_the_frozen_flow_rates(self):
        crossing = run(start=datetime(2026, 7, 31, 23, 50, tzinfo=UTC), minutes=20, flow=95.5)
        piece = clip(crossing, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
        assert piece.flow_lpm == 95.5


class TestCoverage:
    def test_full_coverage_without_gaps(self):
        assert coverage_percent(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), []
        ) == 100.0

    def test_gap_reduces_coverage_proportionally(self):
        percent = coverage_percent(
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 2, tzinfo=UTC),
            [
                Interval(
                    datetime(2026, 7, 1, 0, tzinfo=UTC),
                    datetime(2026, 7, 1, 2, 24, tzinfo=UTC),
                )
            ],
        )
        assert percent == pytest.approx(90.0)

    def test_overlapping_gaps_are_not_counted_twice(self):
        gaps = [
            Interval(datetime(2026, 7, 1, 0, tzinfo=UTC), datetime(2026, 7, 1, 6, tzinfo=UTC)),
            Interval(datetime(2026, 7, 1, 3, tzinfo=UTC), datetime(2026, 7, 1, 6, tzinfo=UTC)),
        ]
        percent = coverage_percent(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), gaps
        )
        assert percent == pytest.approx(75.0)

    def test_a_gap_reaching_outside_the_period_is_clipped(self):
        gaps = [
            Interval(datetime(2026, 6, 30, tzinfo=UTC), datetime(2026, 7, 1, 6, tzinfo=UTC))
        ]
        percent = coverage_percent(
            datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 2, tzinfo=UTC), gaps
        )
        assert percent == pytest.approx(75.0)

    @pytest.mark.parametrize(
        ("value", "tone"),
        [(99.5, "good"), (97.0, "info"), (90.0, "warn"), (50.0, "bad")],
    )
    def test_grades(self, value, tone):
        assert coverage_grade(value)[1] == tone


class TestAggregates:
    def test_zone_aggregate_collects_runs(self):
        start = datetime(2026, 7, 5, 6, tzinfo=UTC)
        runs = [
            run("a", 1, start, minutes=30),
            run("a", 1, start + timedelta(days=1), minutes=60),
            run("b", 2, start, minutes=45),
        ]
        aggregates = {item.zone_id: item for item in aggregate_zones(runs, "UTC")}
        first = aggregates["a"]
        assert first.run_count == 2
        assert first.runtime_seconds == 5400
        assert first.longest_run_seconds == 3600
        assert first.shortest_run_seconds == 1800
        assert len(first.active_days) == 2
        assert first.average_seconds_per_active_day == 2700
        assert first.share_percent(sum(a.water_liters_estimate for a in aggregates.values())) > 0

    def test_days_include_empty_days(self):
        days = aggregate_days(
            [run(start=datetime(2026, 7, 5, 6, tzinfo=UTC))],
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 8, tzinfo=UTC),
            "UTC",
        )
        assert len(days) == 7
        assert sum(1 for day in days if day.runtime_seconds > 0) == 1

    def test_a_run_across_midnight_is_split_between_days(self):
        crossing = ZoneRun(
            zone_id="a",
            zone_number=1,
            zone_name="أ",
            started_at=datetime(2026, 7, 5, 23, 40, tzinfo=UTC),
            ended_at=datetime(2026, 7, 6, 0, 20, tzinfo=UTC),
            flow_lpm=60.0,
            flow_min_lpm=60.0,
            flow_max_lpm=60.0,
        )
        days = {
            day.day.isoformat(): day
            for day in aggregate_days(
                [crossing],
                datetime(2026, 7, 1, tzinfo=UTC),
                datetime(2026, 7, 8, tzinfo=UTC),
                "UTC",
            )
        }
        assert days["2026-07-05"].runtime_seconds == 1200
        assert days["2026-07-06"].runtime_seconds == 1200
        # عدد التشغيلات يُنسب ليوم البداية فقط، فلا يُحتسب الحدث مرتين.
        assert days["2026-07-05"].run_count == 1
        assert days["2026-07-06"].run_count == 0


class TestPeriodTotals:
    def test_totals_combine_everything(self):
        start = datetime(2026, 7, 5, 10, tzinfo=UTC)
        runs = [run("a", 1, start, minutes=30), run("b", 2, start, minutes=30)]
        totals = period_totals(
            runs,
            period_start=datetime(2026, 7, 1, tzinfo=UTC),
            period_end=datetime(2026, 8, 1, tzinfo=UTC),
            gaps=[],
            estimated_input_kw=4.0,
        )
        assert totals.zone_runtime_seconds == 3600
        assert totals.pump_runtime_seconds == 1800
        assert totals.water_liters_estimate == pytest.approx(8400.0)
        assert totals.energy_kwh == pytest.approx(2.0)
        assert totals.event_count == 2
        assert totals.coverage_percent == 100.0

    def test_energy_follows_the_pump_not_the_sum_of_zones(self):
        assert energy_kwh(3600, 4.0) == pytest.approx(4.0)

    def test_low_confidence_runs_are_counted(self):
        totals = period_totals(
            [run(confidence="low"), run("b", 2, confidence="high")],
            period_start=datetime(2026, 7, 1, tzinfo=UTC),
            period_end=datetime(2026, 8, 1, tzinfo=UTC),
            gaps=[],
            estimated_input_kw=4.0,
        )
        assert totals.low_confidence_runs == 1
