"""Exception hierarchy for the Hydrawise client."""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "HydrawiseError",
    "HydrawiseConnectionError",
    "HydrawiseAPIError",
    "HydrawiseAuthError",
    "HydrawiseRateLimitError",
]


class HydrawiseError(Exception):
    """Base class for every error raised by this package."""


class HydrawiseConnectionError(HydrawiseError):
    """The request never produced an HTTP response (DNS, TCP, TLS, timeout)."""


class HydrawiseAPIError(HydrawiseError):
    """The API answered, but the answer was an error.

    Hydrawise reports most failures as HTTP 200 with an ``error_msg`` field in
    the JSON body, so ``status_code`` is often 200 even here.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = dict(payload) if payload is not None else None


class HydrawiseAuthError(HydrawiseAPIError):
    """The API key was missing, malformed or rejected."""


class HydrawiseRateLimitError(HydrawiseAPIError):
    """The API key exceeded its request allowance.

    ``retry_after`` is the number of seconds the server asked us to wait, when
    it said so; ``None`` when it did not.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, payload=payload)
        self.retry_after = retry_after
