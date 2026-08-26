"""The polling loop that keeps the local run log up to date.

Run this as a service (systemd timer, cron, a container — anything that keeps
it alive) so no watering goes unrecorded. Polling every 60s is plenty: the
shortest run a zone can be given is a minute, and the client honours the
server's own ``nextpoll`` hint on top of the interval set here.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .client import HydrawiseClient
from .errors import HydrawiseConnectionError, HydrawiseRateLimitError, HydrawiseError
from .storage import RunEvent, RunStore

__all__ = ["poll_once", "poll_forever", "PollOutcome"]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class PollOutcome:
    """The result of one loop iteration."""

    polled_at: datetime
    events: List[RunEvent]
    error: Optional[Exception] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def poll_once(
    client: HydrawiseClient,
    store: RunStore,
    *,
    controller_id: Optional[int] = None,
    now: Optional[datetime] = None,
    max_gap_seconds: float = 900.0,
) -> List[RunEvent]:
    """Fetch the current status and fold it into the run log."""
    moment = now or _utcnow()
    # A run last seen long ago belongs to a poller that died mid-run; close it
    # at its last observation rather than letting the gap inflate it.
    store.close_stale(now=moment, max_gap_seconds=max_gap_seconds)
    status = client.status_schedule(controller_id, force=True)
    return store.record_status(status, now=moment, controller_id=controller_id)


def poll_forever(
    client: HydrawiseClient,
    store: RunStore,
    *,
    interval: float = 60.0,
    controller_id: Optional[int] = None,
    on_outcome: Optional[Callable[[PollOutcome], None]] = None,
    sleep: Callable[[float], None] = _time.sleep,
    now: Callable[[], datetime] = _utcnow,
    max_iterations: Optional[int] = None,
    stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Poll until interrupted, returning the number of iterations completed.

    Transient failures — a rate limit, a dropped connection — are reported
    through ``on_outcome`` and retried on the next tick with a widened
    interval, rather than ending the loop.
    """
    iterations = 0
    backoff = 0.0
    while True:
        if stop is not None and stop():
            break
        if max_iterations is not None and iterations >= max_iterations:
            break
        moment = now()
        try:
            events = poll_once(client, store, controller_id=controller_id, now=moment)
            outcome = PollOutcome(moment, events)
            backoff = 0.0
        except HydrawiseRateLimitError as exc:
            outcome = PollOutcome(moment, [], exc)
            backoff = max(backoff * 2 or interval, float(exc.retry_after or interval))
        except (HydrawiseConnectionError, HydrawiseError) as exc:
            outcome = PollOutcome(moment, [], exc)
            backoff = min(600.0, max(backoff * 2, interval))
        iterations += 1
        if on_outcome is not None:
            on_outcome(outcome)
        if max_iterations is not None and iterations >= max_iterations:
            break
        if stop is not None and stop():
            break
        sleep(max(interval, backoff))
    return iterations
