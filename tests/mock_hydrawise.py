"""خادم Hydrawise وهمي عبر ``httpx.MockTransport`` (SRS §31: اختبر قبل الحساب الحقيقي)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

CUSTOMER_DETAILS: dict[str, Any] = {
    "controller_id": 4242,
    "customer_id": 1337,
    "current_controller": "ALMOHANA",
    "controllers": [
        {
            "name": "ALMOHANA",
            "last_contact": 1_755_000_000,
            "serial_number": "000000000ABC",
            "controller_id": 4242,
            "status": "All good!",
        }
    ],
}


def relay(
    relay_id: int,
    number: int,
    name: str,
    *,
    running: bool = False,
    run: int | str = 1800,
    time_to_next: int = 3600,
) -> dict[str, Any]:
    """محبس واحد. أثناء التشغيل ``time == 1`` و``run`` هي الثواني المتبقية."""
    return {
        "relay_id": relay_id,
        "relay": number,
        "name": name,
        "time": 1 if running else time_to_next,
        "run": run,
        "timestr": "Tue",
        "nicetime": "Tuesday, 12 August 6:00am",
        "lastwater": "1 day ago",
        "period": 86400,
        "type": 106,
    }


def status_schedule(
    relays: list[dict[str, Any]],
    *,
    nextpoll: int | None = 60,
    epoch: int = 1_755_000_000,
    running: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "controller_id": 4242,
        "customer_id": 1337,
        "name": "ALMOHANA",
        "status": "All good!",
        "message": "",
        "time": epoch,
        "relays": relays,
        "sensors": [],
        "running": running if running is not None else [],
    }
    if nextpoll is not None:
        payload["nextpoll"] = nextpoll
    return payload


@dataclass
class MockHydrawise:
    """يعيد استجابات مبرمجة ويسجّل كل طلب.

    ``responses`` طابور: كل استدعاء يستهلك عنصرًا، وآخر عنصر يتكرر إلى
    الأبد — فيسهل وصف تسلسل عينات ثم تثبيت الحالة الأخيرة.
    """

    status_responses: list[Any] = field(default_factory=list)
    customer_response: Any = field(default_factory=lambda: CUSTOMER_DETAILS)
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ------------------------------------------------------------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "customerdetails.php":
            return self._respond(self.customer_response)
        if endpoint == "statusschedule.php":
            if not self.status_responses:
                return httpx.Response(200, json=status_schedule([]))
            item = (
                self.status_responses.pop(0)
                if len(self.status_responses) > 1
                else self.status_responses[0]
            )
            return self._respond(item)
        return httpx.Response(404, json={"error_msg": "unknown endpoint"})

    @staticmethod
    def _respond(item: Any) -> httpx.Response:
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, int):
            return httpx.Response(item, text="error")
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    # ------------------------------------------------------------------
    @property
    def api_keys_seen(self) -> set[str]:
        return {
            request.url.params.get("api_key", "")
            for request in self.requests
            if "api_key" in request.url.params
        }

    def endpoints(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(request.content or b"{}") for request in self.requests]
