"""Canned API payloads, shaped like what the Hydrawise cloud actually returns."""

from __future__ import annotations

import json
from typing import Any, Dict

CUSTOMER_DETAILS: Dict[str, Any] = {
    "controller_id": 4242,
    "customer_id": 1337,
    "current_controller": "Home Controller",
    "controllers": [
        {
            "name": "Home Controller",
            "last_contact": 1755000000,
            "serial_number": "000000000ABC",
            "controller_id": 4242,
            "status": "All good!",
            "latitude": "24.7136",
            "longitude": "46.6753",
        },
        {
            "name": "Farm Controller",
            "last_contact": 1754900000,
            "serial_number": "000000000DEF",
            "controller_id": 4243,
            "status": "Unknown",
        },
    ],
}

STATUS_SCHEDULE: Dict[str, Any] = {
    "nextpoll": 60,
    "message": "",
    "controller_id": 4242,
    "customer_id": 1337,
    "name": "Home Controller",
    "status": "All good!",
    "time": 1755000000,
    "sensors": [
        {
            "input": 0,
            "type": 1,
            "mode": 1,
            "timer": 0,
            "offtimer": 0,
            "name": "Rain sensor",
            "offlevel": 0,
            "relays": [{"id": 100001}, {"id": 100002}],
        }
    ],
    "relays": [
        {
            "relay_id": 100001,
            "time": 3600,
            "type": 106,
            "run": "1800",
            "relay": 1,
            "name": "Front lawn",
            "period": 86400,
            "timestr": "Tue",
            "nicetime": "Tuesday, 12 August 6:00am",
            "lastwater": "1 day ago",
        },
        {
            "relay_id": 100002,
            "time": 1576800000,
            "type": 106,
            "run": "2700",
            "relay": 2,
            "name": "Date palms",
            "period": 259200,
            "timestr": "Not scheduled",
            "nicetime": "Not scheduled",
            "lastwater": "3 days ago",
        },
        {
            "relay_id": 100003,
            "time": 7200,
            "type": 106,
            "run": "900",
            "relay": 3,
            "name": "Vegetable beds",
            "period": 86400,
            "timestr": "Tue",
            "nicetime": "Tuesday, 12 August 7:00am",
            "lastwater": "12 hours ago",
        },
    ],
    "running": [
        {
            "relay_id": 100001,
            "relay": 1,
            "name": "Front lawn",
            "time_left": 600,
            "run": 1800,
        }
    ],
}

STATUS_SCHEDULE_IDLE: Dict[str, Any] = {
    **STATUS_SCHEDULE,
    "time": 1755001200,
    "running": [],
}

SETZONE_OK: Dict[str, Any] = {
    "message": "Running Front lawn for 30 minutes",
    "message_type": "info",
}

ERROR_BAD_KEY: Dict[str, Any] = {"error_msg": "Invalid API key", "error": 1}
ERROR_RATE_LIMIT: Dict[str, Any] = {"error_msg": "API rate limit exceeded"}


def body(payload: Dict[str, Any]) -> str:
    return json.dumps(payload)
