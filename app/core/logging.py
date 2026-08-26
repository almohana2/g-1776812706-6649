"""تسجيل منظَّم بصيغة JSON مع إخفاء الأسرار (SRS §5.3، §15.3، §NFR-004).

قاعدتان صارمتان يفرضهما هذا الملف:

* لا يُكتب عنوان URL كاملًا لأي طلب Hydrawise — المفتاح يسافر في Query String.
* أي سر معروف من الإعدادات يُستبدل بـ``***`` قبل الكتابة، حتى لو تسرّب إلى
  نص رسالة خطأ قادم من مكتبة خارجية.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from app.core.config import get_settings

_QUERY_SECRET = re.compile(r"(api_key|apikey|token|password|secret)=([^&\s\"']+)", re.IGNORECASE)
_PHONE = re.compile(r"\b(\d{3})(\d{4,})(\d{4})\b")

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}


def mask_phone(number: str) -> str:
    """``96812345218`` → ``968****5218`` (SRS §15.3)."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) <= 7:
        return "*" * len(digits)
    return f"{digits[:3]}{'*' * (len(digits) - 7)}{digits[-4:]}"


def redact(text: str) -> str:
    """يزيل الأسرار من نص حر قبل تسجيله."""
    if not text:
        return text
    cleaned = _QUERY_SECRET.sub(r"\1=***", text)
    for secret in get_settings().secret_values():
        cleaned = cleaned.replace(secret, "***")
    return _PHONE.sub(lambda m: f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}", cleaned)


class JsonFormatter(logging.Formatter):
    """سطر JSON واحد لكل سجل، مع الحقول الإضافية التي يمررها المتصل."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """يهيّئ الجذر مرة واحدة بصيغة JSON على stdout."""
    settings = get_settings()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    # هذه المكتبات تسجّل عناوين URL كاملة افتراضيًا — وفيها المفتاح.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
