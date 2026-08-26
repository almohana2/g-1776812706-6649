"""Site configuration: who owns which valve, and what water and power cost.

Hydrawise knows how long each valve ran. It does not know your valves' flow
rates, your pump's power draw, your tariffs, or who is paying for which
valve — so all of that lives in a JSON file next to the run log.

Secrets are referenced by environment variable name, never stored inline:
``api_key_env`` and ``email.password_env`` name the variables to read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "ZoneConfig",
    "PersonConfig",
    "EmailConfig",
    "Config",
    "ConfigError",
    "EXAMPLE_CONFIG",
]


class ConfigError(ValueError):
    """The configuration file is missing something, or contradicts itself."""


def _as_float(value: Any, name: str, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _as_int(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a whole number, got {value!r}") from exc


@dataclass
class ZoneConfig:
    """One valve: how fast it flows, what it costs to drive, and whose it is.

    ``flow_rate_lpm`` is litres per minute — read it off the valve's nozzle
    chart, or measure it once with a bucket and a stopwatch. Without it a zone
    contributes run hours to the report but no water volume.
    """

    relay_id: Optional[int] = None
    zone: Optional[int] = None
    name: Optional[str] = None
    flow_rate_lpm: Optional[float] = None
    pump_kw: Optional[float] = None
    owner: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ZoneConfig":
        relay_id = _as_int(payload.get("relay_id"), "zones[].relay_id")
        zone = _as_int(payload.get("zone"), "zones[].zone")
        if relay_id is None and zone is None:
            raise ConfigError("each zone needs a relay_id or a zone number")
        return cls(
            relay_id=relay_id,
            zone=zone,
            name=payload.get("name"),
            flow_rate_lpm=_as_float(payload.get("flow_rate_lpm"), "zones[].flow_rate_lpm"),
            pump_kw=_as_float(payload.get("pump_kw"), "zones[].pump_kw"),
            owner=payload.get("owner"),
        )

    def matches(self, relay_id: Optional[int], zone_number: Optional[int]) -> bool:
        if self.relay_id is not None and relay_id is not None:
            return self.relay_id == relay_id
        if self.zone is not None and zone_number is not None:
            return self.zone == zone_number
        return False

    @property
    def key(self) -> str:
        if self.relay_id is not None:
            return f"relay:{self.relay_id}"
        return f"zone:{self.zone}"


@dataclass
class PersonConfig:
    """Someone who receives a monthly bill."""

    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    language: str = "en"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PersonConfig":
        person_id = payload.get("id") or payload.get("name")
        if not person_id:
            raise ConfigError("each person needs an id")
        language = str(payload.get("language") or "en").lower()
        if language not in {"en", "ar"}:
            raise ConfigError("person language must be 'en' or 'ar'")
        return cls(
            id=str(person_id),
            name=payload.get("name") or str(person_id),
            email=payload.get("email"),
            language=language,
        )

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class EmailConfig:
    """SMTP settings for the monthly send.

    The password is read from ``password_env`` at send time so it never lands
    in the config file or in git.
    """

    smtp_host: Optional[str] = None
    smtp_port: int = 587
    username: Optional[str] = None
    password_env: str = "HYDRAWISE_SMTP_PASSWORD"
    from_address: Optional[str] = None
    use_starttls: bool = True
    use_ssl: bool = False
    bcc: List[str] = field(default_factory=list)
    subject_template: str = "Irrigation report — {period} — {name}"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EmailConfig":
        bcc = payload.get("bcc") or []
        if isinstance(bcc, str):
            bcc = [bcc]
        return cls(
            smtp_host=payload.get("smtp_host"),
            smtp_port=int(payload.get("smtp_port") or 587),
            username=payload.get("username"),
            password_env=payload.get("password_env") or "HYDRAWISE_SMTP_PASSWORD",
            from_address=payload.get("from_address") or payload.get("username"),
            use_starttls=bool(payload.get("use_starttls", True)),
            use_ssl=bool(payload.get("use_ssl", False)),
            bcc=[str(item) for item in bcc],
            subject_template=payload.get("subject_template")
            or "Irrigation report — {period} — {name}",
        )

    @property
    def password(self) -> Optional[str]:
        return os.environ.get(self.password_env)

    @property
    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.from_address)


@dataclass
class Config:
    """The whole site: tariffs, valves, people and mail settings."""

    api_key_env: str = "HYDRAWISE_API_KEY"
    controller_id: Optional[int] = None
    timezone: Optional[str] = None
    currency: str = ""
    water_tariff_per_m3: float = 0.0
    electricity_tariff_per_kwh: float = 0.0
    default_pump_kw: float = 0.0
    default_flow_rate_lpm: Optional[float] = None
    zones: List[ZoneConfig] = field(default_factory=list)
    people: List[PersonConfig] = field(default_factory=list)
    email: EmailConfig = field(default_factory=EmailConfig)
    path: Optional[str] = None

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Config":
        file_path = Path(path).expanduser()
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"no config file at {file_path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{file_path} is not valid JSON: {exc}") from exc
        config = cls.from_dict(payload)
        config.path = str(file_path)
        return config

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Config":
        if not isinstance(payload, Mapping):
            raise ConfigError("the config file must contain a JSON object")
        water = payload.get("water") or {}
        electricity = payload.get("electricity") or {}
        zones = [
            ZoneConfig.from_dict(item)
            for item in payload.get("zones") or []
            if isinstance(item, Mapping)
        ]
        people = [
            PersonConfig.from_dict(item)
            for item in payload.get("people") or []
            if isinstance(item, Mapping)
        ]
        config = cls(
            api_key_env=payload.get("api_key_env") or "HYDRAWISE_API_KEY",
            controller_id=_as_int(payload.get("controller_id"), "controller_id"),
            timezone=payload.get("timezone"),
            currency=payload.get("currency") or "",
            water_tariff_per_m3=_as_float(
                water.get("tariff_per_m3"), "water.tariff_per_m3", 0.0
            )
            or 0.0,
            electricity_tariff_per_kwh=_as_float(
                electricity.get("tariff_per_kwh"), "electricity.tariff_per_kwh", 0.0
            )
            or 0.0,
            default_pump_kw=_as_float(
                electricity.get("default_pump_kw"), "electricity.default_pump_kw", 0.0
            )
            or 0.0,
            default_flow_rate_lpm=_as_float(
                water.get("default_flow_rate_lpm"), "water.default_flow_rate_lpm"
            ),
            zones=zones,
            people=people,
            email=EmailConfig.from_dict(payload.get("email") or {}),
        )
        config.validate()
        return config

    def validate(self) -> None:
        ids = [person.id for person in self.people]
        duplicates = {item for item in ids if ids.count(item) > 1}
        if duplicates:
            raise ConfigError(f"duplicate person ids: {', '.join(sorted(duplicates))}")
        known = set(ids)
        for zone in self.zones:
            if zone.owner is not None and zone.owner not in known:
                raise ConfigError(
                    f"zone {zone.key} is owned by unknown person {zone.owner!r}"
                )

    # ------------------------------------------------------------------
    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env)

    def zone_for(
        self, relay_id: Optional[int], zone_number: Optional[int]
    ) -> Optional[ZoneConfig]:
        for zone in self.zones:
            if zone.matches(relay_id, zone_number):
                return zone
        return None

    def person(self, person_id: Optional[str]) -> Optional[PersonConfig]:
        if person_id is None:
            return None
        for person in self.people:
            if person.id == person_id:
                return person
        return None

    def flow_rate_for(self, zone: Optional[ZoneConfig]) -> Optional[float]:
        if zone is not None and zone.flow_rate_lpm is not None:
            return zone.flow_rate_lpm
        return self.default_flow_rate_lpm

    def pump_kw_for(self, zone: Optional[ZoneConfig]) -> float:
        if zone is not None and zone.pump_kw is not None:
            return zone.pump_kw
        return self.default_pump_kw


EXAMPLE_CONFIG: Dict[str, Any] = {
    "api_key_env": "HYDRAWISE_API_KEY",
    "controller_id": None,
    "timezone": "Asia/Riyadh",
    "currency": "SAR",
    "water": {
        "tariff_per_m3": 3.0,
        "default_flow_rate_lpm": None,
    },
    "electricity": {
        "tariff_per_kwh": 0.18,
        "default_pump_kw": 2.2,
    },
    "people": [
        {"id": "ahmed", "name": "Ahmed", "email": "ahmed@example.com", "language": "ar"},
        {"id": "sara", "name": "Sara", "email": "sara@example.com", "language": "en"},
    ],
    "zones": [
        {
            "zone": 1,
            "name": "Front lawn",
            "flow_rate_lpm": 40.0,
            "pump_kw": 2.2,
            "owner": "ahmed",
        },
        {
            "zone": 2,
            "name": "Date palms",
            "flow_rate_lpm": 60.0,
            "pump_kw": 2.2,
            "owner": "sara",
        },
    ],
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "you@example.com",
        "password_env": "HYDRAWISE_SMTP_PASSWORD",
        "from_address": "you@example.com",
        "use_starttls": True,
        "bcc": [],
        "subject_template": "Irrigation report — {period} — {name}",
    },
}
