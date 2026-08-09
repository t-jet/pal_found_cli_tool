"""Safe classification helpers for optional Foundry SDK exceptions."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_SDK_STATUS_BY_CLASS = {
    "UnauthorizedError": 401,
    "PermissionDeniedError": 403,
    "NotFoundError": 404,
    "RateLimitError": 429,
    "ServiceUnavailable": 503,
}


def sdk_http_status(exception: BaseException) -> int | None:
    """Return HTTP status carried by a response or native SDK error class."""
    response = getattr(exception, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    try:
        errors: Any = import_module("foundry_sdk._errors")
    except ImportError:
        return None
    for class_name, class_status in _SDK_STATUS_BY_CLASS.items():
        error_class = getattr(errors, class_name, None)
        if (
            isinstance(error_class, type)
            and issubclass(error_class, BaseException)
            and isinstance(exception, error_class)
        ):
            return class_status
    return None
