"""A dependency-free client for the Hydrawise REST API v1.

Only three endpoints exist in v1, all of them ``GET`` requests authenticated
with an ``api_key`` query parameter:

``customerdetails.php``
    the account and its controllers
``statusschedule.php``
    zones, sensors, and what is watering right now
``setzone.php``
    run / stop / suspend commands

The API key comes from ``app.hydrawise.com`` under *My Account → Account
Details*. It is a bearer credential in query-string clothing: anyone holding
it can water your garden, so keep it out of source control.
"""

from __future__ import annotations

import json
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from .errors import (
    HydrawiseAPIError,
    HydrawiseAuthError,
    HydrawiseConnectionError,
    HydrawiseRateLimitError,
)
from .models import CommandResult, CustomerDetails, StatusSchedule

__all__ = ["HttpResponse", "HydrawiseClient", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://api.hydrawise.com/api/v1"
DEFAULT_TIMEOUT = 15.0
USER_AGENT = "hydrawise-report/1.0 (+https://github.com/almohana2)"

#: ``setzone.php`` needs a period id alongside a custom duration; 999 is the
#: documented "use the custom value" sentinel.
CUSTOM_PERIOD_ID = 999

_RUN_ACTIONS = {"run", "runall"}
_ALL_ACTIONS = {"runall", "stopall", "suspendall"}


@dataclass(frozen=True)
class HttpResponse:
    """The minimum an HTTP transport has to report back."""

    status: int
    body: str
    headers: Mapping[str, str] = None  # type: ignore[assignment]

    def header(self, name: str) -> Optional[str]:
        if not self.headers:
            return None
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


#: A transport takes a URL and a timeout and returns an :class:`HttpResponse`.
#: Injecting one is how the tests run without a network.
Transport = Callable[[str, float], HttpResponse]

Deadline = Union[datetime, timedelta, int, float]


def urllib_transport(url: str, timeout: float) -> HttpResponse:
    """The default transport, built on :mod:`urllib.request`."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return HttpResponse(
                status=response.status, body=body, headers=dict(response.headers)
            )
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
        body = exc.read().decode("utf-8", errors="replace")
        return HttpResponse(status=exc.code, body=body, headers=dict(exc.headers or {}))
    except urllib.error.URLError as exc:
        raise HydrawiseConnectionError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HydrawiseConnectionError(f"timed out after {timeout}s: {url}") from exc


class HydrawiseClient:
    """Talks to the Hydrawise cloud on behalf of one API key.

    The client is deliberately conservative about request volume. Hydrawise
    throttles per API key and answers a key that polls too eagerly with HTTP
    429, so:

    * ``min_request_interval`` spaces consecutive requests apart;
    * :meth:`status_schedule` caches its answer and honours the ``nextpoll``
      hint the server returns, refetching only once that window has passed
      (``force=True`` overrides);
    * 429 and 5xx responses are retried ``max_retries`` times with backoff,
      preferring the server's ``Retry-After`` when it sends one.

    ``transport``, ``sleep`` and ``monotonic`` exist so tests can drive all of
    that with a fake clock.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        min_request_interval: float = 1.0,
        poll_interval_floor: float = 30.0,
        max_retries: int = 2,
        transport: Optional[Transport] = None,
        sleep: Callable[[float], None] = _time.sleep,
        monotonic: Callable[[], float] = _time.monotonic,
    ) -> None:
        if not api_key or not api_key.strip():
            raise HydrawiseAuthError("an API key is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_request_interval = max(0.0, min_request_interval)
        self.poll_interval_floor = max(0.0, poll_interval_floor)
        self.max_retries = max(0, max_retries)
        self._transport = transport or urllib_transport
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: Optional[float] = None
        self._status_cache: Dict[
            Optional[int], Tuple[float, float, StatusSchedule]
        ] = {}

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def customer_details(self, *, type: str = "controllers") -> CustomerDetails:
        """Fetch the account and the controllers attached to it."""
        payload = self.request("customerdetails.php", {"type": type})
        return CustomerDetails.from_api(payload)

    def status_schedule(
        self, controller_id: Optional[int] = None, *, force: bool = False
    ) -> StatusSchedule:
        """Fetch zones, sensors and live runs for one controller.

        Returns the cached response while the server's ``nextpoll`` window is
        still open unless ``force`` is set.
        """
        if not force:
            cached = self._cached_status(controller_id)
            if cached is not None:
                return cached
        params: Dict[str, Any] = {}
        if controller_id is not None:
            params["controller_id"] = controller_id
        payload = self.request("statusschedule.php", params)
        status = StatusSchedule.from_api(payload)
        wait = max(float(status.next_poll or 0), self.poll_interval_floor)
        self._status_cache[controller_id] = (self._monotonic(), wait, status)
        return status

    def next_status_poll_in(self, controller_id: Optional[int] = None) -> float:
        """Seconds until :meth:`status_schedule` would hit the network again."""
        entry = self._status_cache.get(controller_id)
        if entry is None:
            return 0.0
        fetched_at, wait, _ = entry
        return max(0.0, fetched_at + wait - self._monotonic())

    # ------------------------------------------------------------------
    # zone commands
    # ------------------------------------------------------------------
    def run_zone(
        self, relay_id: int, seconds: Optional[int] = None
    ) -> CommandResult:
        """Start one zone; ``seconds=None`` uses the zone's programmed time."""
        return self._set_zone("run", relay_id=relay_id, custom=seconds)

    def run_all_zones(self, seconds: Optional[int] = None) -> CommandResult:
        """Start every zone in sequence."""
        return self._set_zone("runall", custom=seconds)

    def stop_zone(self, relay_id: int) -> CommandResult:
        """Stop one zone that is watering."""
        return self._set_zone("stop", relay_id=relay_id)

    def stop_all_zones(self) -> CommandResult:
        """Stop everything that is watering."""
        return self._set_zone("stopall")

    def suspend_zone(self, relay_id: int, until: Deadline) -> CommandResult:
        """Suspend one zone until ``until`` (datetime, timedelta or epoch)."""
        return self._set_zone(
            "suspend", relay_id=relay_id, custom=_to_epoch(until), allow_zero=True
        )

    def suspend_all_zones(self, until: Deadline) -> CommandResult:
        """Suspend every zone until ``until``."""
        return self._set_zone("suspendall", custom=_to_epoch(until), allow_zero=True)

    def resume_zone(self, relay_id: int) -> CommandResult:
        """Clear a suspension on one zone."""
        return self._set_zone("suspend", relay_id=relay_id, custom=0, allow_zero=True)

    def resume_all_zones(self) -> CommandResult:
        """Clear suspensions on every zone."""
        return self._set_zone("suspendall", custom=0, allow_zero=True)

    def _set_zone(
        self,
        action: str,
        *,
        relay_id: Optional[int] = None,
        custom: Optional[int] = None,
        allow_zero: bool = False,
    ) -> CommandResult:
        params: Dict[str, Any] = {"action": action}
        if action in _ALL_ACTIONS:
            if relay_id is not None:
                raise ValueError(f"{action} does not take a relay_id")
        else:
            if relay_id is None:
                raise ValueError(f"{action} requires a relay_id")
            params["relay_id"] = int(relay_id)
        if custom is not None:
            custom = int(custom)
            if custom < 0 or (custom == 0 and not allow_zero):
                raise ValueError("custom duration must be a positive number of seconds")
            params["period_id"] = CUSTOM_PERIOD_ID
            params["custom"] = custom
        elif action in _RUN_ACTIONS:
            # No custom value: the controller falls back to the programmed run
            # time for the zone, which is what the app's "Run now" does.
            pass
        payload = self.request("setzone.php", params)
        result = CommandResult.from_api(payload)
        # A command changes what statusschedule would say, so drop the cache.
        self._status_cache.clear()
        return result

    # ------------------------------------------------------------------
    # transport plumbing
    # ------------------------------------------------------------------
    def request(self, endpoint: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Issue one authenticated GET and return the decoded JSON object."""
        url = self._build_url(endpoint, params or {})
        attempt = 0
        while True:
            self._throttle()
            response = self._transport(url, self.timeout)
            self._last_request_at = self._monotonic()
            retry_after = self._retry_delay(response, attempt)
            if retry_after is not None and attempt < self.max_retries:
                attempt += 1
                self._sleep(retry_after)
                continue
            return self._decode(response, url)

    def _build_url(self, endpoint: str, params: Mapping[str, Any]) -> str:
        query = {"api_key": self.api_key}
        for key, value in params.items():
            if value is None:
                continue
            query[key] = str(value)
        return f"{self.base_url}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(query)}"

    def _throttle(self) -> None:
        if self._last_request_at is None or self.min_request_interval <= 0:
            return
        elapsed = self._monotonic() - self._last_request_at
        remaining = self.min_request_interval - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _retry_delay(self, response: HttpResponse, attempt: int) -> Optional[float]:
        """How long to wait before retrying, or ``None`` to stop retrying."""
        if response.status != 429 and response.status < 500:
            return None
        header = response.header("Retry-After")
        if header:
            try:
                return max(0.0, float(header.strip()))
            except ValueError:
                pass
        return min(60.0, 2.0 ** attempt * self.min_request_interval or 1.0)

    def _decode(self, response: HttpResponse, url: str) -> Dict[str, Any]:
        safe_url = url.replace(self.api_key, "***")
        if response.status == 429:
            raise HydrawiseRateLimitError(
                "Hydrawise rate limit exceeded; slow the polling down",
                status_code=response.status,
                retry_after=self._retry_after_seconds(response),
            )
        if response.status in (401, 403):
            raise HydrawiseAuthError(
                "Hydrawise rejected the API key",
                status_code=response.status,
            )
        if response.status >= 400:
            raise HydrawiseAPIError(
                f"HTTP {response.status} from {safe_url}",
                status_code=response.status,
            )

        body = response.body.strip()
        if not body:
            raise HydrawiseAPIError(
                f"empty response from {safe_url}", status_code=response.status
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HydrawiseAPIError(
                f"non-JSON response from {safe_url}: {body[:200]}",
                status_code=response.status,
            ) from exc
        if not isinstance(payload, dict):
            raise HydrawiseAPIError(
                f"expected a JSON object from {safe_url}, got {type(payload).__name__}",
                status_code=response.status,
            )

        error = payload.get("error_msg") or payload.get("error")
        if error:
            self._raise_for_error(str(error), response.status, payload)
        return payload

    @staticmethod
    def _retry_after_seconds(response: HttpResponse) -> Optional[float]:
        header = response.header("Retry-After")
        if not header:
            return None
        try:
            return float(header.strip())
        except ValueError:
            return None

    @staticmethod
    def _raise_for_error(
        message: str, status_code: int, payload: Mapping[str, Any]
    ) -> None:
        lowered = message.lower()
        if "api key" in lowered or "apikey" in lowered or "unauthor" in lowered:
            raise HydrawiseAuthError(message, status_code=status_code, payload=payload)
        if "rate" in lowered or "too many" in lowered or "limit" in lowered:
            raise HydrawiseRateLimitError(
                message, status_code=status_code, payload=payload
            )
        raise HydrawiseAPIError(message, status_code=status_code, payload=payload)

    def _cached_status(self, controller_id: Optional[int]) -> Optional[StatusSchedule]:
        entry = self._status_cache.get(controller_id)
        if entry is None:
            return None
        fetched_at, wait, status = entry
        if self._monotonic() - fetched_at < wait:
            return status
        return None


def _to_epoch(value: Deadline) -> int:
    """Normalise a suspend deadline to a Unix timestamp.

    Naive datetimes are read as local time, which is how the Hydrawise app
    behaves; timedeltas and plain numbers below one year are read as offsets
    from now.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.astimezone()
        return int(moment.timestamp())
    if isinstance(value, timedelta):
        return int(_time.time() + value.total_seconds())
    number = float(value)
    if number <= 0:
        return 0
    if number < 31_536_000:  # anything under a year is an offset, not an epoch
        return int(_time.time() + number)
    return int(number)


def utcnow() -> datetime:
    """Timezone-aware ``now`` in UTC (``datetime.utcnow`` is deprecated)."""
    return datetime.now(tz=timezone.utc)
