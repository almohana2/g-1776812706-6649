"""Test doubles: a scripted HTTP transport and a fake clock."""

from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional, Sequence

from hydrawise.client import HttpResponse, HydrawiseClient

from . import fixtures


class FakeTransport:
    """Returns queued responses and records every URL it was asked for."""

    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self.responses: List[HttpResponse] = list(responses)
        self.urls: List[str] = []

    @classmethod
    def json(cls, *payloads: Dict[str, Any], status: int = 200) -> "FakeTransport":
        return cls([HttpResponse(status, fixtures.body(payload), {}) for payload in payloads])

    def __call__(self, url: str, timeout: float) -> HttpResponse:
        self.urls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    # -- assertions helpers ------------------------------------------------
    def query(self, index: int = -1) -> Dict[str, str]:
        parsed = urllib.parse.urlparse(self.urls[index])
        return {
            key: value[0]
            for key, value in urllib.parse.parse_qs(parsed.query).items()
        }

    def endpoint(self, index: int = -1) -> str:
        return urllib.parse.urlparse(self.urls[index]).path.rsplit("/", 1)[-1]

    @property
    def call_count(self) -> int:
        return len(self.urls)


class FakeClock:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(transport: FakeTransport, clock: Optional[FakeClock] = None, **kwargs: Any) -> HydrawiseClient:
    clock = clock or FakeClock()
    options: Dict[str, Any] = {
        "transport": transport,
        "sleep": clock.sleep,
        "monotonic": clock.monotonic,
        "min_request_interval": 0.0,
    }
    options.update(kwargs)
    return HydrawiseClient("test-key", **options)
