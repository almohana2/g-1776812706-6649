"""A Hydrawise (Hunter) irrigation client, run logger and per-person billing tool.

Quick start::

    from hydrawise import HydrawiseClient

    client = HydrawiseClient(api_key)
    status = client.status_schedule()
    for zone in status.zones:
        print(zone.number, zone.name, zone.nice_time)

See ``README.md`` for the reporting pipeline: poll into a local run log, then
turn logged run time into cubic metres, kWh and a monthly bill per person.
"""

from .client import DEFAULT_BASE_URL, HydrawiseClient
from .config import Config, ConfigError, EmailConfig, PersonConfig, ZoneConfig
from .errors import (
    HydrawiseAPIError,
    HydrawiseAuthError,
    HydrawiseConnectionError,
    HydrawiseError,
    HydrawiseRateLimitError,
)
from .models import (
    CommandResult,
    Controller,
    CustomerDetails,
    RunningZone,
    Sensor,
    StatusSchedule,
    Zone,
)
from .poller import poll_forever, poll_once
from .storage import RunRecord, RunStore
from .usage import PersonUsage, UsageReport, ZoneUsage, build_report, month_bounds

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "DEFAULT_BASE_URL",
    "HydrawiseClient",
    "Config",
    "ConfigError",
    "EmailConfig",
    "PersonConfig",
    "ZoneConfig",
    "HydrawiseError",
    "HydrawiseAPIError",
    "HydrawiseAuthError",
    "HydrawiseConnectionError",
    "HydrawiseRateLimitError",
    "CommandResult",
    "Controller",
    "CustomerDetails",
    "RunningZone",
    "Sensor",
    "StatusSchedule",
    "Zone",
    "RunRecord",
    "RunStore",
    "poll_once",
    "poll_forever",
    "PersonUsage",
    "UsageReport",
    "ZoneUsage",
    "build_report",
    "month_bounds",
]
