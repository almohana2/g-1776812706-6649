"""توليد التقارير من قاعدة البيانات (SRS §FR-006..FR-008، AC-007، AC-012)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import (
    Confidence,
    DataGap,
    GapReason,
    MonthlyReport,
    ZoneRuntimeEvent,
)
from app.services.report_generator import (
    build_monthly_payload,
    generate_daily_summary,
    generate_monthly_report,
)

MUSCAT_OFFSET = timedelta(hours=4)


def muscat(year, month, day, hour=0, minute=0) -> datetime:
    """وقت محلي في مسقط، مخزَّن كـUTC كما تفعل قاعدة البيانات."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - MUSCAT_OFFSET


def add_event(db, zone, start: datetime, minutes: int, *, flow=Decimal("140.00")):
    end = start + timedelta(minutes=minutes)
    event = ZoneRuntimeEvent(
        zone_id=zone.id,
        started_at=start,
        ended_at=end,
        last_running_at=end,
        runtime_seconds=minutes * 60,
        confidence=Confidence.HIGH,
        flow_rate_lpm_snapshot=flow,
        flow_min_lpm_snapshot=Decimal("80.00"),
        flow_max_lpm_snapshot=Decimal("200.00"),
        water_liters_estimate=Decimal(minutes) * flow,
    )
    db.add(event)
    return event


class TestMonthlyPayload:
    def test_totals_follow_the_documented_formulas(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        add_event(db, zones[1], muscat(2026, 7, 6, 6), 30)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        metrics = payload["metrics"]

        assert metrics["zone_runtime_seconds"] == 5400
        assert metrics["pump_runtime_seconds"] == 5400  # لا تداخل
        assert metrics["water_estimate_liters"] == pytest.approx(12600.0)
        assert metrics["water_min_liters"] == pytest.approx(7200.0)
        assert metrics["water_max_liters"] == pytest.approx(18000.0)
        assert metrics["energy_estimate_kwh"] == pytest.approx(6.0)
        assert metrics["event_count"] == 2

    def test_overlapping_runs_count_once_for_the_pump(self, db, controller, zones):
        start = muscat(2026, 7, 5, 10)
        add_event(db, zones[0], start, 30)
        add_event(db, zones[1], start, 30)
        db.commit()

        metrics = build_monthly_payload(db, controller, year=2026, month=7)["metrics"]
        assert metrics["zone_runtime_seconds"] == 3600
        assert metrics["pump_runtime_seconds"] == 1800

    def test_ac_007_a_run_across_the_month_boundary_is_split(self, db, controller, zones):
        # 23:50 آخر يوليو بتوقيت مسقط حتى 00:10 من أغسطس.
        add_event(db, zones[0], muscat(2026, 7, 31, 23, 50), 20)
        db.commit()

        july = build_monthly_payload(db, controller, year=2026, month=7)["metrics"]
        august = build_monthly_payload(db, controller, year=2026, month=8)["metrics"]

        assert july["zone_runtime_seconds"] == 600
        assert august["zone_runtime_seconds"] == 600

    def test_zone_rows_carry_the_documented_columns(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        add_event(db, zones[0], muscat(2026, 7, 6, 6), 20)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        row = next(item for item in payload["zones"] if item["number"] == 1)
        assert row["run_count"] == 2
        assert row["runtime_seconds"] == 4800
        assert row["longest_run_seconds"] == 3600
        assert row["shortest_run_seconds"] == 1200
        assert row["active_days"] == 2
        assert row["average_seconds_per_active_day"] == 2400
        assert row["share_percent"] == pytest.approx(100.0)

    def test_daily_series_covers_every_day_of_the_month(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        db.commit()
        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert len(payload["daily"]) == 31
        assert sum(day["run_count"] for day in payload["daily"]) == 1

    def test_ac_012_a_gap_lowers_coverage_and_is_reported(self, db, controller, zones):
        start = muscat(2026, 7, 10, 0)
        db.add(
            DataGap(
                controller_id=controller.id,
                started_at=start,
                ended_at=start + timedelta(days=6),
                reason=GapReason.WORKER_DOWN,
                duration_seconds=6 * 86400,
                may_affect_runtime=True,
            )
        )
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert payload["coverage"]["percent"] < 85
        assert payload["coverage"]["grade"] == "غير مكتملة"
        assert "worker_down" in payload["coverage"]["reasons"]

    def test_a_month_without_events_is_marked_not_zero(self, db, controller, zones):
        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert payload["has_data"] is False
        assert payload["metrics"]["water_estimate_liters"] == 0

    def test_an_open_event_is_counted_only_up_to_now(self, db, controller, zones):
        now = datetime.now(tz=UTC)
        db.add(
            ZoneRuntimeEvent(
                zone_id=zones[0].id,
                started_at=now - timedelta(minutes=10),
                last_running_at=now,
                confidence=Confidence.HIGH,
                flow_rate_lpm_snapshot=Decimal("140.00"),
                flow_min_lpm_snapshot=Decimal("80.00"),
                flow_max_lpm_snapshot=Decimal("200.00"),
            )
        )
        db.commit()

        payload = build_monthly_payload(db, controller, year=now.year, month=now.month, now=now)
        assert 500 <= payload["metrics"]["zone_runtime_seconds"] <= 700

    def test_comparison_with_the_previous_month(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 6, 5, 6), 60)
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 90)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert payload["comparison"]["month"] == "2026-06"
        assert payload["comparison"]["water_change_percent"] == pytest.approx(50.0)

    def test_the_disclaimer_and_methodology_are_always_included(self, db, controller, zones):
        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert payload["methodology"]["water_is_estimated"] is True
        assert payload["methodology"]["energy_is_estimated"] is True
        assert "تقديري" in payload["disclaimer"]
        assert payload["methodology"]["uncalibrated_zones"]


class TestAlerts:
    def test_a_zone_idle_this_month_but_active_last_month_is_flagged(self, db, controller, zones):
        add_event(db, zones[1], muscat(2026, 6, 5, 6), 60)
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        codes = {alert["code"] for alert in payload["alerts"]}
        assert "zone_idle" in codes

    def test_a_big_monthly_increase_is_flagged(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 6, 5, 6), 30)
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 90)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert "monthly_increase" in {alert["code"] for alert in payload["alerts"]}

    def test_many_very_short_runs_in_a_day_are_flagged(self, db, controller, zones):
        base = muscat(2026, 7, 5, 6)
        for index in range(12):
            add_event(db, zones[0], base + timedelta(minutes=5 * index), 1)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        assert "short_runs" in {alert["code"] for alert in payload["alerts"]}

    def test_concurrent_runs_are_reported_as_informational(self, db, controller, zones):
        start = muscat(2026, 7, 5, 10)
        add_event(db, zones[0], start, 30)
        add_event(db, zones[1], start + timedelta(minutes=10), 30)
        db.commit()

        payload = build_monthly_payload(db, controller, year=2026, month=7)
        alert = next(
            item for item in payload["alerts"] if item["code"] == "concurrent_runs"
        )
        assert alert["severity"] == "info"


class TestPersistence:
    def test_generate_stores_a_frozen_snapshot(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        db.commit()

        report = generate_monthly_report(db, controller, year=2026, month=7)
        db.commit()

        assert report.month == date(2026, 7, 1)
        assert report.total_zone_runtime_seconds == 3600
        assert report.summary_json["metrics"]["water_estimate_liters"] == pytest.approx(8400.0)

    def test_changing_a_flow_rate_later_does_not_move_a_published_report(
        self, db, controller, zones
    ):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        db.commit()
        report = generate_monthly_report(db, controller, year=2026, month=7)
        db.commit()
        before = report.summary_json["metrics"]["water_estimate_liters"]

        zones[0].flow_rate_lpm = Decimal("999.00")
        db.commit()
        db.refresh(report)

        assert report.summary_json["metrics"]["water_estimate_liters"] == before

    def test_regenerating_replaces_instead_of_duplicating(self, db, controller, zones):
        add_event(db, zones[0], muscat(2026, 7, 5, 6), 60)
        db.commit()
        first = generate_monthly_report(db, controller, year=2026, month=7)
        db.commit()

        add_event(db, zones[0], muscat(2026, 7, 6, 6), 60)
        db.commit()
        second = generate_monthly_report(db, controller, year=2026, month=7)
        db.commit()

        assert first.id == second.id
        assert db.query(MonthlyReport).count() == 1
        assert second.total_zone_runtime_seconds == 7200


class TestDailySummary:
    def test_day_bounds_follow_the_site_timezone(self, db, controller, zones):
        # 00:30 بتوقيت مسقط = 20:30 من اليوم السابق بـUTC.
        add_event(db, zones[0], muscat(2026, 7, 5, 0, 30), 30)
        db.commit()

        summary = generate_daily_summary(db, controller, day=date(2026, 7, 5))
        assert summary.totals.event_count == 1
        assert summary.totals.zone_runtime_seconds == 1800

        previous = generate_daily_summary(db, controller, day=date(2026, 7, 4))
        assert previous.totals.event_count == 0

    def test_a_gap_in_the_day_is_surfaced(self, db, controller, zones):
        start = muscat(2026, 7, 5, 2)
        db.add(
            DataGap(
                controller_id=controller.id,
                started_at=start,
                ended_at=start + timedelta(hours=3),
                reason=GapReason.NETWORK,
                duration_seconds=10800,
            )
        )
        db.commit()

        summary = generate_daily_summary(db, controller, day=date(2026, 7, 5))
        assert summary.has_gap
        assert summary.gap_seconds == pytest.approx(10800)
        assert "network" in summary.gap_reasons
