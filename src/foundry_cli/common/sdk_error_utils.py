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

# Ordered so subclasses with more specific ADR behavior win before their bases.
_SDK_EXIT_BY_CLASS = (
    ("UnauthorizedError", 2),
    ("NotAuthenticated", 2),
    ("PermissionDeniedError", 3),
    ("ApiNotFoundError", 4),
    ("NotFoundError", 4),
    ("BadRequestError", 1),
    ("ConflictError", 1),
    ("RequestEntityTooLargeError", 1),
    ("UnprocessableEntityError", 1),
    ("EnvironmentNotConfigured", 9),
    ("TimeoutError", 5),
    ("RateLimitError", 7),
    ("ServiceUnavailable", 6),
    ("ConnectionError", 6),
    ("InternalServerError", 6),
    ("SDKInternalError", 6),
    ("SseContentTypeError", 6),
    ("SseEventDecodeError", 6),
    ("StreamConsumedError", 6),
    ("PalantirException", 6),
)


def _sdk_errors() -> Any | None:
    """Load the optional SDK error module without making SDK availability mandatory."""
    try:
        return import_module("foundry_sdk._errors")
    except ImportError:
        return None


def sdk_exception_exit_code(exception: BaseException) -> int | None:
    """Map an installed Foundry SDK exception to the ADR-001 taxonomy."""
    errors = _sdk_errors()
    if errors is None:
        return None
    for class_name, exit_code in _SDK_EXIT_BY_CLASS:
        error_class = getattr(errors, class_name, None)
        if (
            isinstance(error_class, type)
            and issubclass(error_class, BaseException)
            and isinstance(exception, error_class)
        ):
            return exit_code
    return None


def sdk_exception_exit_map() -> dict[type[BaseException], int]:
    """Return installed SDK exception classes and their ADR-001 exit codes."""
    errors = _sdk_errors()
    if errors is None:
        return {}
    mapping: dict[type[BaseException], int] = {}
    for class_name, exit_code in _SDK_EXIT_BY_CLASS:
        error_class = getattr(errors, class_name, None)
        if isinstance(error_class, type) and issubclass(error_class, BaseException):
            mapping[error_class] = exit_code
    return mapping


def is_sdk_retryable_transport(exception: BaseException) -> bool:
    """Return whether a native SDK timeout or connection failure is retryable."""
    errors = _sdk_errors()
    if errors is None:
        return False
    for class_name in ("TimeoutError", "ConnectionError"):
        error_class = getattr(errors, class_name, None)
        if (
            isinstance(error_class, type)
            and issubclass(error_class, BaseException)
            and isinstance(exception, error_class)
        ):
            return True
    return False


def sdk_http_status(exception: BaseException) -> int | None:
    """Return HTTP status carried by a response or native SDK error class."""
    response = getattr(exception, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    errors = _sdk_errors()
    if errors is None:
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
