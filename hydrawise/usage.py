"""Turn logged run time into water volume, energy, cost — and a bill per person.

The two conversions are deliberately simple and stated out loud in the report,
because both rest on numbers the controller cannot measure:

``m³ = flow_rate_lpm × minutes ÷ 1000``
    the valve's rated flow, from its nozzle chart or a bucket test.
``kWh = pump_kw × hours``
    the pump's draw while that valve is open.

Where a flow rate or a pump rating is missing the run still shows up as time,
and the corresponding volume/energy column reads as unknown rather than zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from .config import Config, PersonConfig, ZoneConfig
from .storage import RunRecord

__all__ = [
    "ZoneUsage",
    "PersonUsage",
    "UsageReport",
    "build_report",
    "month_bounds",
    "parse_month",
    "previous_month",
]


def parse_month(text: str) -> Tuple[int, int]:
    """Parse ``YYYY-MM`` into ``(year, month)``."""
    parts = text.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"expected a month as YYYY-MM, got {text!r}")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"expected a month as YYYY-MM, got {text!r}") from exc
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in {text!r}")
    return year, month


def _tzinfo(name: Optional[str]):
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # unknown zone, or no tzdata on this platform
        return timezone.utc


def month_bounds(
    month: str, timezone_name: Optional[str] = None
) -> Tuple[datetime, datetime]:
    """The UTC half-open interval ``[start, end)`` covering a local month."""
    year, month_number = parse_month(month)
    tzinfo = _tzinfo(timezone_name)
    start_local = datetime(year, month_number, 1, tzinfo=tzinfo)
    if month_number == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tzinfo)
    else:
        end_local = datetime(year, month_number + 1, 1, tzinfo=tzinfo)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def previous_month(today: datetime, timezone_name: Optional[str] = None) -> str:
    """The ``YYYY-MM`` before the one ``today`` falls in."""
    local = today.astimezone(_tzinfo(timezone_name))
    first = local.replace(day=1)
    last_month = first - timedelta(days=1)
    return f"{last_month.year:04d}-{last_month.month:02d}"


@dataclass
class ZoneUsage:
    """What one valve consumed during the period."""

    key: str
    name: str
    relay_id: Optional[int]
    zone_number: Optional[int]
    owner_id: Optional[str]
    runs: int = 0
    seconds: int = 0
    flow_rate_lpm: Optional[float] = None
    pump_kw: Optional[float] = None
    water_tariff_per_m3: float = 0.0
    electricity_tariff_per_kwh: float = 0.0

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @property
    def minutes(self) -> float:
        return self.seconds / 60.0

    @property
    def cubic_meters(self) -> Optional[float]:
        if self.flow_rate_lpm is None:
            return None
        return self.flow_rate_lpm * self.minutes / 1000.0

    @property
    def kwh(self) -> Optional[float]:
        if not self.pump_kw:
            return None
        return self.pump_kw * self.hours

    @property
    def water_cost(self) -> float:
        volume = self.cubic_meters
        return (volume or 0.0) * self.water_tariff_per_m3

    @property
    def energy_cost(self) -> float:
        energy = self.kwh
        return (energy or 0.0) * self.electricity_tariff_per_kwh

    @property
    def total_cost(self) -> float:
        return self.water_cost + self.energy_cost


@dataclass
class PersonUsage:
    """One person's share of the period: their valves, summed."""

    person: PersonConfig
    zones: List[ZoneUsage] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        return sum(zone.seconds for zone in self.zones)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @property
    def runs(self) -> int:
        return sum(zone.runs for zone in self.zones)

    @property
    def cubic_meters(self) -> float:
        return sum(zone.cubic_meters or 0.0 for zone in self.zones)

    @property
    def kwh(self) -> float:
        return sum(zone.kwh or 0.0 for zone in self.zones)

    @property
    def water_cost(self) -> float:
        return sum(zone.water_cost for zone in self.zones)

    @property
    def energy_cost(self) -> float:
        return sum(zone.energy_cost for zone in self.zones)

    @property
    def total_cost(self) -> float:
        return self.water_cost + self.energy_cost

    @property
    def has_estimates(self) -> bool:
        """``True`` when every zone had the inputs needed to price it."""
        return all(
            zone.cubic_meters is not None and zone.kwh is not None
            for zone in self.zones
        )

    def share_of(self, total: float, value: float) -> float:
        return 0.0 if total <= 0 else value / total * 100.0


@dataclass
class UsageReport:
    """The whole period: per person, plus valves nobody claimed."""

    period: str
    start: datetime
    end: datetime
    currency: str
    people: List[PersonUsage] = field(default_factory=list)
    unassigned: List[ZoneUsage] = field(default_factory=list)
    generated_at: Optional[datetime] = None
    warnings: List[str] = field(default_factory=list)

    @property
    def all_zones(self) -> List[ZoneUsage]:
        return [zone for person in self.people for zone in person.zones] + self.unassigned

    @property
    def seconds(self) -> int:
        return sum(zone.seconds for zone in self.all_zones)

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    @property
    def cubic_meters(self) -> float:
        return sum(zone.cubic_meters or 0.0 for zone in self.all_zones)

    @property
    def kwh(self) -> float:
        return sum(zone.kwh or 0.0 for zone in self.all_zones)

    @property
    def water_cost(self) -> float:
        return sum(zone.water_cost for zone in self.all_zones)

    @property
    def energy_cost(self) -> float:
        return sum(zone.energy_cost for zone in self.all_zones)

    @property
    def total_cost(self) -> float:
        return self.water_cost + self.energy_cost

    def person(self, person_id: str) -> Optional[PersonUsage]:
        for entry in self.people:
            if entry.person.id == person_id:
                return entry
        return None


def _zone_label(
    zone_config: Optional[ZoneConfig], record_name: Optional[str], zone_number: Optional[int],
    relay_id: Optional[int],
) -> str:
    if zone_config is not None and zone_config.name:
        return zone_config.name
    if record_name:
        return record_name
    if zone_number is not None:
        return f"Zone {zone_number}"
    return f"Relay {relay_id}"


def build_report(
    runs: Iterable[RunRecord],
    config: Config,
    *,
    period: str,
    start: datetime,
    end: datetime,
    generated_at: Optional[datetime] = None,
) -> UsageReport:
    """Aggregate run records into a per-person usage report."""
    buckets: Dict[str, ZoneUsage] = {}
    order: List[str] = []

    # Seed every configured zone first, so the report keeps the config's order
    # and a person who watered nothing still gets a bill that says zero.
    for zone_config in config.zones:
        buckets[zone_config.key] = ZoneUsage(
            key=zone_config.key,
            name=_zone_label(zone_config, None, zone_config.zone, zone_config.relay_id),
            relay_id=zone_config.relay_id,
            zone_number=zone_config.zone,
            owner_id=zone_config.owner,
            flow_rate_lpm=config.flow_rate_for(zone_config),
            pump_kw=config.pump_kw_for(zone_config) or None,
            water_tariff_per_m3=config.water_tariff_per_m3,
            electricity_tariff_per_kwh=config.electricity_tariff_per_kwh,
        )
        order.append(zone_config.key)

    for record in runs:
        zone_config = config.zone_for(record.relay_id, record.zone_number)
        key = zone_config.key if zone_config else f"relay:{record.relay_id}"
        usage = buckets.get(key)
        if usage is None:
            usage = ZoneUsage(
                key=key,
                name=_zone_label(
                    zone_config, record.zone_name, record.zone_number, record.relay_id
                ),
                relay_id=record.relay_id,
                zone_number=record.zone_number
                if record.zone_number is not None
                else (zone_config.zone if zone_config else None),
                owner_id=zone_config.owner if zone_config else None,
                flow_rate_lpm=config.flow_rate_for(zone_config),
                pump_kw=config.pump_kw_for(zone_config) or None,
                water_tariff_per_m3=config.water_tariff_per_m3,
                electricity_tariff_per_kwh=config.electricity_tariff_per_kwh,
            )
            buckets[key] = usage
            order.append(key)
        usage.runs += 1
        usage.seconds += max(0, record.seconds)

    people = [PersonUsage(person=person) for person in config.people]
    by_id = {entry.person.id: entry for entry in people}
    unassigned: List[ZoneUsage] = []
    for key in order:
        usage = buckets[key]
        owner = by_id.get(usage.owner_id) if usage.owner_id else None
        if owner is None:
            unassigned.append(usage)
        else:
            owner.zones.append(usage)

    warnings: List[str] = []
    missing_flow = [
        usage.name for usage in buckets.values() if usage.flow_rate_lpm is None
    ]
    if missing_flow:
        warnings.append(
            "No flow rate configured for: "
            + ", ".join(sorted(missing_flow))
            + " — their water volume is not counted."
        )
    if not config.electricity_tariff_per_kwh and any(
        usage.kwh for usage in buckets.values()
    ):
        warnings.append("No electricity tariff configured — energy is shown, not priced.")
    if unassigned and any(usage.seconds for usage in unassigned):
        warnings.append(
            "Unassigned zones with usage: "
            + ", ".join(sorted(usage.name for usage in unassigned if usage.seconds))
        )

    return UsageReport(
        period=period,
        start=start,
        end=end,
        currency=config.currency,
        people=people,
        unassigned=unassigned,
        generated_at=generated_at,
        warnings=warnings,
    )
