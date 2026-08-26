"""آلة حالات أحداث التشغيل (SRS §9، AC-002)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.enums import Confidence
from app.schemas.hydrawise import StatusSchedulePayload
from app.services.event_engine import (
    MAX_REASONABLE_RUNTIME_SECONDS,
    OpenRunState,
    ZoneObservation,
    gap_threshold_seconds,
    observations_from_payload,
    plan_close,
    plan_extend,
    plan_stale_close,
    plan_start,
)
from tests.mock_hydrawise import relay, status_schedule

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


def observation(**kwargs) -> ZoneObservation:
    base = {
        "relay_id": 100001,
        "physical_number": 1,
        "name": "المسطح الأمامي",
        "is_running": True,
    }
    base.update(kwargs)
    return ZoneObservation(**base)


class TestObservations:
    def test_time_equal_one_means_running(self):
        payload = StatusSchedulePayload.model_validate(
            status_schedule([relay(100001, 1, "أ", running=True, run=900)])
        )
        [item] = observations_from_payload(payload)
        assert item.is_running
        assert item.remaining_seconds == 900

    def test_idle_relay_reports_next_run_duration_as_planned(self):
        payload = StatusSchedulePayload.model_validate(
            status_schedule([relay(100001, 1, "أ", running=False, run=1800)])
        )
        [item] = observations_from_payload(payload)
        assert not item.is_running
        assert item.planned_seconds == 1800

    def test_run_arriving_as_string_is_coerced(self):
        # حالة اختبار SRS §24.4
        payload = StatusSchedulePayload.model_validate(
            status_schedule([relay(100001, 1, "أ", running=True, run="450")])
        )
        [item] = observations_from_payload(payload)
        assert item.remaining_seconds == 450

    def test_running_array_supplies_planned_and_remaining(self):
        payload = StatusSchedulePayload.model_validate(
            status_schedule(
                [relay(100001, 1, "أ", running=False, run=1800)],
                running=[
                    {"relay_id": 100001, "relay": 1, "name": "أ", "time_left": 300, "run": 1800}
                ],
            )
        )
        [item] = observations_from_payload(payload)
        assert item.is_running
        assert item.remaining_seconds == 300
        assert item.planned_seconds == 1800

    def test_relay_without_id_is_ignored(self):
        payload = StatusSchedulePayload.model_validate(
            status_schedule([{"relay": 1, "name": "بلا معرّف", "time": 1}])
        )
        assert observations_from_payload(payload) == []

    def test_arabic_zone_names_survive(self):
        payload = StatusSchedulePayload.model_validate(
            status_schedule([relay(100001, 1, "حديقة النخيل — الخلفية")])
        )
        [item] = observations_from_payload(payload)
        assert item.name == "حديقة النخيل — الخلفية"


class TestPlanStart:
    def test_backdates_by_elapsed_when_within_one_poll(self):
        decision = plan_start(
            observation(remaining_seconds=1740, planned_seconds=1800),
            observed_at=T0,
            previous_nextpoll_seconds=60,
        )
        assert decision.started_at == T0 - timedelta(seconds=60)
        assert decision.confidence is Confidence.HIGH

    def test_clamps_to_two_poll_intervals_and_lowers_confidence(self):
        # مضى نصف ساعة على التشغيل، لكن أطول ما يمكن أن نكون فوّتناه دقيقتان.
        decision = plan_start(
            observation(remaining_seconds=0 + 1, planned_seconds=1800),
            observed_at=T0,
            previous_nextpoll_seconds=60,
        )
        assert decision.started_at == T0 - timedelta(seconds=120)
        assert decision.confidence is Confidence.MEDIUM

    def test_manual_run_without_planned_time_starts_now_with_low_confidence(self):
        decision = plan_start(
            observation(remaining_seconds=None, planned_seconds=None),
            observed_at=T0,
            previous_nextpoll_seconds=60,
        )
        assert decision.started_at == T0
        assert decision.confidence is Confidence.LOW

    def test_uses_planned_time_remembered_from_the_previous_sample(self):
        decision = plan_start(
            observation(remaining_seconds=1700, planned_seconds=None),
            observed_at=T0,
            previous_planned_seconds=1800,
            previous_nextpoll_seconds=120,
        )
        assert decision.planned_runtime_seconds == 1800
        assert decision.started_at == T0 - timedelta(seconds=100)

    def test_remaining_greater_than_planned_is_not_trusted(self):
        decision = plan_start(
            observation(remaining_seconds=5000, planned_seconds=1800),
            observed_at=T0,
            previous_nextpoll_seconds=60,
        )
        assert decision.confidence is Confidence.LOW
        assert decision.started_at == T0


class TestPlanExtend:
    def test_keeps_start_and_refreshes_remaining(self):
        state = OpenRunState(started_at=T0, last_running_at=T0, last_remaining_seconds=1800)
        update = plan_extend(
            state, observation(remaining_seconds=900), observed_at=T0 + timedelta(minutes=15)
        )
        assert update.last_running_at == T0 + timedelta(minutes=15)
        assert update.last_remaining_seconds == 900

    def test_missing_remaining_keeps_the_last_known_value(self):
        state = OpenRunState(started_at=T0, last_running_at=T0, last_remaining_seconds=1200)
        update = plan_extend(
            state, observation(remaining_seconds=None), observed_at=T0 + timedelta(minutes=1)
        )
        assert update.last_remaining_seconds == 1200


class TestPlanClose:
    def test_uses_last_remaining_to_pin_the_end(self):
        state = OpenRunState(
            started_at=T0,
            last_running_at=T0 + timedelta(minutes=25),
            last_remaining_seconds=300,
            confidence=Confidence.HIGH,
        )
        decision = plan_close(state, observed_at=T0 + timedelta(minutes=31))
        assert decision.ended_at == T0 + timedelta(minutes=30)
        assert decision.runtime_seconds == 1800
        assert decision.confidence is Confidence.HIGH

    def test_falls_back_to_observation_time_and_downgrades(self):
        state = OpenRunState(
            started_at=T0,
            last_running_at=T0 + timedelta(minutes=10),
            last_remaining_seconds=None,
            confidence=Confidence.HIGH,
        )
        decision = plan_close(state, observed_at=T0 + timedelta(minutes=11))
        assert decision.ended_at == T0 + timedelta(minutes=11)
        assert decision.confidence is Confidence.MEDIUM

    def test_a_gap_since_the_last_sighting_downgrades_further(self):
        state = OpenRunState(
            started_at=T0,
            last_running_at=T0 + timedelta(minutes=10),
            last_remaining_seconds=None,
            confidence=Confidence.HIGH,
        )
        decision = plan_close(
            state, observed_at=T0 + timedelta(hours=2), gap_since_last_seen=True
        )
        assert decision.confidence is Confidence.LOW

    def test_never_ends_before_it_started(self):
        state = OpenRunState(
            started_at=T0, last_running_at=T0 - timedelta(minutes=5), last_remaining_seconds=0
        )
        decision = plan_close(state, observed_at=T0 - timedelta(minutes=1))
        assert decision.ended_at == T0
        assert decision.runtime_seconds == 0

    def test_absurd_runtime_is_capped(self):
        state = OpenRunState(
            started_at=T0, last_running_at=T0 + timedelta(days=3), last_remaining_seconds=None
        )
        decision = plan_close(state, observed_at=T0 + timedelta(days=3))
        assert decision.runtime_seconds == MAX_REASONABLE_RUNTIME_SECONDS
        assert decision.confidence is Confidence.LOW


class TestStaleClose:
    def test_counts_only_observed_time_when_remaining_unknown(self):
        state = OpenRunState(
            started_at=T0,
            last_running_at=T0 + timedelta(minutes=5),
            last_remaining_seconds=None,
            confidence=Confidence.HIGH,
        )
        decision = plan_stale_close(state, now=T0 + timedelta(hours=6))
        assert decision.runtime_seconds == 300
        assert decision.confidence is Confidence.MEDIUM

    def test_extends_only_by_the_known_remaining(self):
        state = OpenRunState(
            started_at=T0,
            last_running_at=T0 + timedelta(minutes=5),
            last_remaining_seconds=1500,
        )
        decision = plan_stale_close(state, now=T0 + timedelta(hours=6))
        assert decision.runtime_seconds == 1800


@pytest.mark.parametrize(
    ("nextpoll", "expected"),
    [(60, 180), (120, 360), (None, 180), (30, 180)],
)
def test_gap_threshold(nextpoll, expected):
    assert gap_threshold_seconds(nextpoll) == expected
