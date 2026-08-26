"""Typed views over the Hydrawise REST API v1 JSON payloads.

The API is loosely typed: numbers arrive as strings about as often as they
arrive as numbers, and undocumented keys come and go between firmware
versions. Every model therefore parses defensively and keeps the payload it
was built from in ``raw``, so callers never lose access to a field this
package does not know about yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "NEVER_SECONDS",
    "Controller",
    "CustomerDetails",
    "Sensor",
    "Zone",
    "RunningZone",
    "StatusSchedule",
    "CommandResult",
]

#: Hydrawise reports "no run scheduled" as a run that starts ~50 years out.
NEVER_SECONDS = 1576800000


def _as_int(value: Any) -> Optional[int]:
    """Coerce an API value to ``int``, or ``None`` when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_datetime(value: Any) -> Optional[datetime]:
    """Interpret an API epoch value as an aware UTC datetime."""
    seconds = _as_int(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class Controller:
    """One controller from ``customerdetails.php?type=controllers``."""

    controller_id: Optional[int] = None
    name: Optional[str] = None
    serial_number: Optional[str] = None
    status: Optional[str] = None
    last_contact: Optional[datetime] = None
    online: Optional[bool] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    address: Optional[str] = None
    timezone: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "Controller":
        status = _as_str(payload.get("status"))
        online = payload.get("online")
        if not isinstance(online, bool):
            online = None if status is None else status.lower() != "unknown"
        return cls(
            controller_id=_as_int(payload.get("controller_id")),
            name=_as_str(payload.get("name")),
            serial_number=_as_str(payload.get("serial_number")),
            status=status,
            last_contact=_as_datetime(payload.get("last_contact")),
            online=online,
            latitude=_as_str(payload.get("latitude")),
            longitude=_as_str(payload.get("longitude")),
            address=_as_str(payload.get("address")),
            timezone=_as_str(payload.get("tz") or payload.get("timezone")),
            raw=dict(payload),
        )


@dataclass
class CustomerDetails:
    """The ``customerdetails.php`` response."""

    customer_id: Optional[int] = None
    controller_id: Optional[int] = None
    current_controller: Optional[str] = None
    controllers: List[Controller] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "CustomerDetails":
        controllers = [
            Controller.from_api(item)
            for item in payload.get("controllers") or []
            if isinstance(item, Mapping)
        ]
        return cls(
            customer_id=_as_int(payload.get("customer_id")),
            controller_id=_as_int(payload.get("controller_id")),
            current_controller=_as_str(payload.get("current_controller")),
            controllers=controllers,
            raw=dict(payload),
        )

    def controller(self, controller_id: int) -> Optional[Controller]:
        for controller in self.controllers:
            if controller.controller_id == controller_id:
                return controller
        return None


@dataclass
class Sensor:
    """A flow or rain sensor attached to a controller."""

    input: Optional[int] = None
    type: Optional[int] = None
    mode: Optional[int] = None
    name: Optional[str] = None
    timer: Optional[int] = None
    offtimer: Optional[int] = None
    offlevel: Optional[int] = None
    relay_ids: List[int] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "Sensor":
        relay_ids: List[int] = []
        for item in payload.get("relays") or []:
            if isinstance(item, Mapping):
                relay_id = _as_int(item.get("id") or item.get("relay_id"))
            else:
                relay_id = _as_int(item)
            if relay_id is not None:
                relay_ids.append(relay_id)
        return cls(
            input=_as_int(payload.get("input")),
            type=_as_int(payload.get("type")),
            mode=_as_int(payload.get("mode")),
            name=_as_str(payload.get("name")),
            timer=_as_int(payload.get("timer")),
            offtimer=_as_int(payload.get("offtimer")),
            offlevel=_as_int(payload.get("offlevel")),
            relay_ids=relay_ids,
            raw=dict(payload),
        )


@dataclass
class Zone:
    """One irrigation zone (the API calls it a *relay*).

    ``relay_id`` is the account-wide identifier commands are addressed to;
    ``number`` is the position on the controller face (1..n) that the
    Hydrawise app shows.
    """

    relay_id: Optional[int] = None
    number: Optional[int] = None
    name: Optional[str] = None
    seconds_until_next_run: Optional[int] = None
    next_run_at: Optional[datetime] = None
    next_run_seconds: Optional[int] = None
    last_water: Optional[str] = None
    time_string: Optional[str] = None
    nice_time: Optional[str] = None
    period: Optional[int] = None
    type: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(
        cls, payload: Mapping[str, Any], *, now: Optional[datetime] = None
    ) -> "Zone":
        seconds = _as_int(payload.get("time"))
        next_run_at = None
        if now is not None and seconds is not None and seconds < NEVER_SECONDS:
            next_run_at = now + timedelta(seconds=seconds)
        return cls(
            relay_id=_as_int(payload.get("relay_id")),
            number=_as_int(payload.get("relay")),
            name=_as_str(payload.get("name")),
            seconds_until_next_run=seconds,
            next_run_at=next_run_at,
            next_run_seconds=_as_int(payload.get("run") or payload.get("run_seconds")),
            last_water=_as_str(payload.get("lastwater")),
            time_string=_as_str(payload.get("timestr")),
            nice_time=_as_str(payload.get("nicetime")),
            period=_as_int(payload.get("period")),
            type=_as_int(payload.get("type")),
            raw=dict(payload),
        )

    @property
    def is_scheduled(self) -> bool:
        """``True`` when a next run is on the calendar.

        Suspended zones and zones with no program report the ~50 year
        placeholder instead of a real offset.
        """
        seconds = self.seconds_until_next_run
        return seconds is not None and seconds < NEVER_SECONDS

    @property
    def is_suspended(self) -> bool:
        """``True`` when the zone is parked with no next run.

        The v1 API has no explicit suspended flag; a zone suspended from the
        app or by :meth:`~hydrawise.client.HydrawiseClient.suspend_zone` is
        reported with the placeholder offset and a ``timestr`` of ``"Not
        scheduled"``.
        """
        return not self.is_scheduled

    @property
    def next_run_duration(self) -> Optional[timedelta]:
        if self.next_run_seconds is None:
            return None
        return timedelta(seconds=self.next_run_seconds)


@dataclass
class RunningZone:
    """An entry of the ``running`` array: a zone watering right now."""

    relay_id: Optional[int] = None
    number: Optional[int] = None
    name: Optional[str] = None
    time_left: Optional[int] = None
    run_seconds: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "RunningZone":
        return cls(
            relay_id=_as_int(payload.get("relay_id")),
            number=_as_int(payload.get("relay")),
            name=_as_str(payload.get("name")),
            time_left=_as_int(payload.get("time_left")),
            run_seconds=_as_int(payload.get("run")),
            raw=dict(payload),
        )

    @property
    def time_remaining(self) -> Optional[timedelta]:
        if self.time_left is None:
            return None
        return timedelta(seconds=self.time_left)


@dataclass
class StatusSchedule:
    """The ``statusschedule.php`` response: zones, sensors and live runs."""

    controller_id: Optional[int] = None
    customer_id: Optional[int] = None
    name: Optional[str] = None
    status: Optional[str] = None
    message: Optional[str] = None
    server_time: Optional[datetime] = None
    next_poll: Optional[int] = None
    zones: List[Zone] = field(default_factory=list)
    sensors: List[Sensor] = field(default_factory=list)
    running: List[RunningZone] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "StatusSchedule":
        server_time = _as_datetime(payload.get("time"))
        zones = [
            Zone.from_api(item, now=server_time)
            for item in payload.get("relays") or []
            if isinstance(item, Mapping)
        ]
        sensors = [
            Sensor.from_api(item)
            for item in payload.get("sensors") or []
            if isinstance(item, Mapping)
        ]
        running = [
            RunningZone.from_api(item)
            for item in payload.get("running") or []
            if isinstance(item, Mapping)
        ]
        return cls(
            controller_id=_as_int(payload.get("controller_id")),
            customer_id=_as_int(payload.get("customer_id")),
            name=_as_str(payload.get("name")),
            status=_as_str(payload.get("status")),
            message=_as_str(payload.get("message")),
            server_time=server_time,
            next_poll=_as_int(payload.get("nextpoll")),
            zones=zones,
            sensors=sensors,
            running=running,
            raw=dict(payload),
        )

    def zone(self, identifier: Any) -> Optional[Zone]:
        """Find a zone by ``relay_id``, by controller position, or by name.

        Integers match ``relay_id`` first and then ``number``, because
        ``relay_id`` is what commands take. Strings match a zone name exactly
        (case-insensitively) before falling back to a unique substring match.
        """
        zone_id = _as_int(identifier) if not isinstance(identifier, str) else None
        if zone_id is not None:
            for zone in self.zones:
                if zone.relay_id == zone_id:
                    return zone
            for zone in self.zones:
                if zone.number == zone_id:
                    return zone
            return None

        text = str(identifier).strip().lower()
        if not text:
            return None
        for zone in self.zones:
            if zone.name is not None and zone.name.lower() == text:
                return zone
        matches = [
            zone
            for zone in self.zones
            if zone.name is not None and text in zone.name.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            # A bare numeric string still addresses a zone by id or position.
            numeric = _as_int(identifier)
            if numeric is not None:
                return self.zone(numeric)
        return None

    def running_zone(self, relay_id: int) -> Optional[RunningZone]:
        for item in self.running:
            if item.relay_id == relay_id:
                return item
        return None

    def is_running(self, relay_id: int) -> bool:
        return self.running_zone(relay_id) is not None


@dataclass
class CommandResult:
    """The ``setzone.php`` response to a run/stop/suspend command."""

    message: Optional[str] = None
    message_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "CommandResult":
        return cls(
            message=_as_str(payload.get("message")),
            message_type=_as_str(payload.get("message_type")),
            raw=dict(payload),
        )

    @property
    def ok(self) -> bool:
        """``True`` unless the controller flagged the command as an error."""
        if self.message_type is None:
            return True
        return self.message_type.lower() not in {"error", "warn", "warning"}


def parse_sequence(items: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Return the mapping entries of a raw API array, ignoring the rest."""
    return [dict(item) for item in items or [] if isinstance(item, Mapping)]
