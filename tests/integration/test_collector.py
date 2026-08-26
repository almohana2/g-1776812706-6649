"""دورة الجمع الكاملة على قاعدة بيانات حقيقية (SRS §9، AC-001..AC-003)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.models import (
    Confidence,
    Controller,
    DataGap,
    EventSource,
    GapReason,
    PollSample,
    Zone,
    ZoneRuntimeEvent,
)
from app.services.collector import Collector
from app.services.hydrawise_client import HydrawiseClient
from tests.mock_hydrawise import MockHydrawise, relay, status_schedule

T0 = datetime(2026, 8, 1, 6, 0, tzinfo=UTC)


def make_collector(mock: MockHydrawise) -> Collector:
    return Collector(
        HydrawiseClient(
            "test-api-key-abcdef",
            base_url="https://api.hydrawise.test/api/v1/",
            transport=mock.transport(),
        )
    )


def idle(run_seconds: int = 1800) -> dict:
    return status_schedule(
        [
            relay(100001, 1, "المسطح الأمامي", running=False, run=run_seconds),
            relay(100002, 2, "النخيل", running=False, run=2700),
        ]
    )


def running_first(remaining: int) -> dict:
    return status_schedule(
        [
            relay(100001, 1, "المسطح الأمامي", running=True, run=remaining),
            relay(100002, 2, "النخيل", running=False, run=2700),
        ]
    )


class TestDiscovery:
    """AC-001: الكنترولرات والمحابس تظهر بلا إدخال يدوي."""

    async def test_controllers_and_zones_are_discovered(self, db):
        mock = MockHydrawise(status_responses=[idle()])
        collector = make_collector(mock)

        controllers = await collector.sync_controllers(db)
        await collector.poll(db, controllers[0])
        db.commit()

        controller = db.query(Controller).one()
        assert controller.name == "ALMOHANA"
        assert controller.hydrawise_controller_id == 4242
        zones = db.query(Zone).order_by(Zone.physical_number).all()
        assert [zone.name for zone in zones] == ["المسطح الأمامي", "النخيل"]
        assert [zone.physical_number for zone in zones] == [1, 2]

    async def test_new_zones_start_on_the_default_flow_and_are_uncalibrated(self, db):
        mock = MockHydrawise(status_responses=[idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        db.commit()

        zone = db.query(Zone).filter(Zone.physical_number == 1).one()
        assert float(zone.flow_rate_lpm) == 140.0
        assert float(zone.flow_min_lpm) == 80.0
        assert float(zone.flow_max_lpm) == 200.0
        assert not zone.is_calibrated

    async def test_rediscovery_does_not_duplicate(self, db):
        mock = MockHydrawise(status_responses=[idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        await collector.sync_controllers(db)
        await collector.poll(db, controller)
        db.commit()

        assert db.query(Controller).count() == 1
        assert db.query(Zone).count() == 2

    async def test_a_vanished_zone_is_deactivated_not_deleted(self, db):
        mock = MockHydrawise(
            status_responses=[
                idle(),
                status_schedule([relay(100001, 1, "المسطح الأمامي", running=False)]),
                status_schedule([relay(100001, 1, "المسطح الأمامي", running=False)]),
            ]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        for _ in range(3):
            await collector.poll(db, controller)
        db.commit()

        gone = db.query(Zone).filter(Zone.hydrawise_relay_id == 100002).one()
        assert gone.is_active is False
        assert db.query(Zone).count() == 2  # التاريخ محفوظ


class TestRunLifecycle:
    """AC-002: تسلسل idle → running → running → idle ينتج حدثًا واحدًا."""

    async def test_one_event_with_a_sane_duration(self, db):
        mock = MockHydrawise(
            status_responses=[idle(), running_first(1740), running_first(900), idle()]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        for _ in range(4):
            await collector.poll(db, controller)
        db.commit()

        events = db.query(ZoneRuntimeEvent).all()
        assert len(events) == 1
        event = events[0]
        assert event.ended_at is not None
        assert event.runtime_seconds > 0
        assert event.source is EventSource.API_OBSERVED
        assert event.water_liters_estimate > 0

    async def test_the_frozen_flow_rate_is_captured_on_the_event(self, db):
        mock = MockHydrawise(status_responses=[idle(), running_first(1740), idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        for _ in range(3):
            await collector.poll(db, controller)
        db.commit()

        event = db.query(ZoneRuntimeEvent).one()
        assert float(event.flow_rate_lpm_snapshot) == 140.0
        assert float(event.flow_min_lpm_snapshot) == 80.0

    async def test_two_zones_running_produce_two_events(self, db):
        both = status_schedule(
            [
                relay(100001, 1, "المسطح الأمامي", running=True, run=900),
                relay(100002, 2, "النخيل", running=True, run=900),
            ]
        )
        mock = MockHydrawise(status_responses=[idle(), both, idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        for _ in range(3):
            await collector.poll(db, controller)
        db.commit()

        assert db.query(ZoneRuntimeEvent).count() == 2

    async def test_a_manual_run_with_no_prior_schedule_is_low_confidence(self, db):
        # التشغيل اليدوي يظهر فجأة بلا عينة سابقة تحمل المدة المخططة.
        mock = MockHydrawise(
            status_responses=[
                status_schedule(
                    [relay(100001, 1, "المسطح الأمامي", running=True, run=0)]
                ),
                status_schedule([relay(100001, 1, "المسطح الأمامي", running=False, run=0)]),
            ]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        await collector.poll(db, controller)
        db.commit()

        event = db.query(ZoneRuntimeEvent).one()
        assert event.confidence is Confidence.LOW


class TestRestart:
    """AC-003: إعادة تشغيل الـWorker لا تضاعف الحدث ولا تفقده."""

    async def test_an_open_event_survives_a_new_collector_instance(self, db):
        first = MockHydrawise(status_responses=[idle(), running_first(1740)])
        collector = make_collector(first)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        await collector.poll(db, controller)
        db.commit()
        open_event = db.query(ZoneRuntimeEvent).one()
        assert open_event.ended_at is None

        # عملية جديدة تمامًا، بنفس قاعدة البيانات.
        second = MockHydrawise(status_responses=[running_first(900), idle()])
        fresh = make_collector(second)
        await fresh.poll(db, controller)
        await fresh.poll(db, controller)
        db.commit()

        events = db.query(ZoneRuntimeEvent).all()
        assert len(events) == 1
        assert events[0].id == open_event.id
        assert events[0].ended_at is not None

    async def test_the_database_forbids_two_open_events_for_one_zone(self, db, zones):
        from sqlalchemy.exc import IntegrityError

        for _ in range(2):
            db.add(
                ZoneRuntimeEvent(
                    zone_id=zones[0].id,
                    started_at=T0,
                    last_running_at=T0,
                    confidence=Confidence.HIGH,
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    async def test_worker_downtime_is_recorded_as_a_gap(self, db):
        mock = MockHydrawise(status_responses=[idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        db.commit()

        # العودة بعد ست ساعات صمت.
        gap = collector.record_worker_downtime(
            db, controller, now=datetime.now(tz=UTC) + timedelta(hours=6)
        )
        db.commit()
        assert gap is not None
        assert gap.reason is GapReason.WORKER_DOWN
        assert gap.duration_seconds > 3600


class TestFailures:
    async def test_a_network_failure_opens_a_gap_and_backs_off(self, db):
        mock = MockHydrawise(status_responses=[idle(), httpx.ConnectError("no route")])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        outcome = await collector.poll(db, controller)
        db.commit()

        assert not outcome.ok
        assert outcome.error_code == "network"
        assert outcome.next_poll_seconds >= 30
        gap = db.query(DataGap).one()
        assert gap.reason is GapReason.NETWORK
        assert gap.ended_at is None

    async def test_the_gap_closes_on_the_next_success(self, db):
        mock = MockHydrawise(
            status_responses=[idle(), httpx.ConnectError("no route"), idle()]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        for _ in range(3):
            await collector.poll(db, controller)
        db.commit()

        gap = db.query(DataGap).one()
        assert gap.ended_at is not None
        assert gap.duration_seconds >= 0

    async def test_rate_limiting_waits_for_retry_after(self, db):
        mock = MockHydrawise(
            status_responses=[
                idle(),
                httpx.Response(429, headers={"Retry-After": "300"}, text="slow"),
            ]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        outcome = await collector.poll(db, controller)
        db.commit()

        assert outcome.next_poll_seconds == 300
        assert db.query(DataGap).one().reason is GapReason.API_429

    async def test_an_invalid_payload_is_recorded_without_breaking_the_loop(self, db):
        mock = MockHydrawise(
            status_responses=[idle(), httpx.Response(200, text="<html>nope</html>")]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        outcome = await collector.poll(db, controller)
        db.commit()

        assert outcome.error_code == "invalid_payload"
        assert db.query(DataGap).one().reason is GapReason.INVALID_PAYLOAD
        assert db.query(PollSample).filter(PollSample.is_success.is_(False)).count() == 1

    async def test_a_rejected_key_is_fatal_so_the_worker_stops_retrying(self, db):
        mock = MockHydrawise(
            status_responses=[idle(), httpx.Response(200, json={"error_msg": "API key not valid"})]
        )
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        outcome = await collector.poll(db, controller)
        db.commit()

        assert outcome.fatal is True

    async def test_the_stored_sample_never_contains_the_api_key(self, db):
        mock = MockHydrawise(status_responses=[idle()])
        collector = make_collector(mock)
        controller = (await collector.sync_controllers(db))[0]
        await collector.poll(db, controller)
        db.commit()

        sample = db.query(PollSample).one()
        assert "test-api-key-abcdef" not in str(sample.payload)
        assert sample.payload_hash and len(sample.payload_hash) == 64


class TestMaintenance:
    async def test_stale_events_are_closed_without_inventing_time(self, db, zones):
        db.add(
            ZoneRuntimeEvent(
                zone_id=zones[0].id,
                started_at=T0,
                last_running_at=T0 + timedelta(minutes=5),
                last_remaining_seconds=None,
                confidence=Confidence.HIGH,
            )
        )
        db.commit()
        collector = make_collector(MockHydrawise())

        closed = collector.close_stale_events(db, now=T0 + timedelta(hours=6))
        db.commit()

        assert closed == 1
        event = db.query(ZoneRuntimeEvent).one()
        assert event.runtime_seconds == 300
        assert event.source is EventSource.API_INFERRED

    async def test_a_still_running_event_is_left_alone(self, db, zones):
        now = datetime.now(tz=UTC)
        db.add(
            ZoneRuntimeEvent(
                zone_id=zones[0].id,
                started_at=now - timedelta(minutes=2),
                last_running_at=now - timedelta(seconds=30),
                last_remaining_seconds=1500,
                confidence=Confidence.HIGH,
            )
        )
        db.commit()
        collector = make_collector(MockHydrawise())

        assert collector.close_stale_events(db, now=now) == 0

    async def test_old_samples_are_purged_but_events_survive(self, db, controller, zones):
        old = datetime.now(tz=UTC) - timedelta(days=200)
        db.add(
            PollSample(controller_id=controller.id, observed_at=old, is_success=True, payload={})
        )
        db.add(
            ZoneRuntimeEvent(
                zone_id=zones[0].id,
                started_at=old,
                ended_at=old + timedelta(minutes=30),
                runtime_seconds=1800,
                confidence=Confidence.HIGH,
            )
        )
        db.commit()
        collector = make_collector(MockHydrawise())

        deleted = collector.purge_old_samples(db)
        db.commit()

        assert deleted == 1
        assert db.query(PollSample).count() == 0
        assert db.query(ZoneRuntimeEvent).count() == 1
