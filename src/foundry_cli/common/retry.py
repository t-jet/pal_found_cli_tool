#!/usr/bin/env python3
"""RetryHandler with exponential backoff and jitter (ADR-002).

Provides a configurable retry mechanism for async operations with
exponential backoff, optional jitter, and support for both decorator
and context manager protocols.

Environment Variables
---------------------
FOUNDRY_MAX_RETRIES : int
    Maximum number of retry attempts. Default: 3
FOUNDRY_RETRY_BASE_DELAY : float
    Base delay in seconds for exponential backoff. Default: 1.0
FOUNDRY_RETRY_MAX_DELAY : float
    Maximum delay cap in seconds. Default: 30.0
FOUNDRY_RETRY_JITTER : bool
    Enable ±10% random jitter on delays. Default: true

Backoff Formula
---------------
delay = min(base_delay * (2 ^ attempt), max_delay)
With jitter: delay *= (1 + random.uniform(-0.1, 0.1))
"""

import asyncio
import logging
import os
import random
import sys
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any, Callable, Dict, Optional, Set, Tuple, Type, TypeVar, Union

import requests

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Environment variable names
ENV_MAX_RETRIES = "FOUNDRY_MAX_RETRIES"
ENV_RETRY_BASE_DELAY = "FOUNDRY_RETRY_BASE_DELAY"
ENV_RETRY_MAX_DELAY = "FOUNDRY_RETRY_MAX_DELAY"
ENV_RETRY_JITTER = "FOUNDRY_RETRY_JITTER"

# Default values
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0
DEFAULT_JITTER = True

# Default retryable exception types
DEFAULT_RETRY_EXCEPTIONS: Tuple[Type[BaseException], ...] = (
    requests.RequestException,
    requests.ConnectionError,
)


def _parse_bool_env(env_var: str, default: bool) -> bool:
    """Parse a boolean environment variable.

    Parameters
    ----------
    env_var : str
        Environment variable name.
    default : bool
        Default value if not set.

    Returns
    -------
    bool
        Parsed boolean value.
    """
    val = os.environ.get(env_var)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _calculate_delay(
    base_delay: float,
    attempt: int,
    max_delay: float,
    jitter: bool,
) -> float:
    """Calculate retry delay with exponential backoff and optional jitter.

    Formula: delay = min(base_delay * (2 ^ attempt), max_delay)
    With jitter: delay *= (1 + random.uniform(-0.1, 0.1))

    Parameters
    ----------
    base_delay : float
        Base delay in seconds.
    attempt : int
        Current attempt number (0-indexed).
    max_delay : float
        Maximum delay cap in seconds.
    jitter : bool
        Whether to apply random jitter.

    Returns
    -------
    float
        Calculated delay in seconds.
    """
    delay = min(base_delay * (2 ** attempt), max_delay)

    if jitter:
        # ±10% jitter
        jitter_factor = 1.0 + random.uniform(-0.1, 0.1)
        delay = float(delay * jitter_factor)

    return delay


class RetryHandler:
    """Configurable retry handler with exponential backoff and jitter.

    Supports both decorator and async context manager protocols.
    Logs retry attempts to stderr via NDJSON structured logging (ADR-005).

    Parameters
    ----------
    max_retries : int, optional
        Maximum number of retry attempts. Read from env var if not set.
    base_delay : float, optional
        Base delay in seconds for backoff. Read from env var if not set.
    max_delay : float, optional
        Maximum delay cap in seconds. Read from env var if not set.
    jitter : bool, optional
        Enable ±10% random jitter. Read from env var if not set.
    retry_on : tuple of Exception types, optional
        Exception types to retry on. Defaults to HTTP-related exceptions.

    Examples
    --------
    As decorator:
    >>> handler = RetryHandler(max_retries=3)
    >>> @handler
    ... async def call_api():
    ...     return await session.get(url)

    As context manager:
    >>> handler = RetryHandler()
    >>> async with handler as h:
    ...     result = await h.execute(session.get(url))
    """

    def __init__(
        self,
        max_retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        jitter: Optional[bool] = None,
        retry_on: Optional[Tuple[Type[BaseException], ...]] = None,
    ) -> None:
        self.max_retries = max_retries if max_retries is not None else int(
            os.environ.get(ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES)
        )
        self.base_delay = base_delay if base_delay is not None else float(
            os.environ.get(ENV_RETRY_BASE_DELAY, DEFAULT_BASE_DELAY)
        )
        self.max_delay = max_delay if max_delay is not None else float(
            os.environ.get(ENV_RETRY_MAX_DELAY, DEFAULT_MAX_DELAY)
        )
        self.jitter = jitter if jitter is not None else _parse_bool_env(
            ENV_RETRY_JITTER, DEFAULT_JITTER
        )
        self.retry_on = retry_on if retry_on is not None else DEFAULT_RETRY_EXCEPTIONS

    def _should_retry(self, exception: BaseException) -> bool:
        """Check if an exception should trigger a retry.

        Parameters
        ----------
        exception : BaseException
            The exception to check.

        Returns
        -------
        bool
            True if the exception type is in the retry list.
        """
        return isinstance(exception, self.retry_on)

    async def execute(
        self,
        coro_func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an async callable with retry logic.

        Parameters
        ----------
        coro_func : Callable
            Async callable to execute.
        *args
            Positional arguments for the callable.
        **kwargs
            Keyword arguments for the callable.

        Returns
        -------
        Any
            Result from the callable.

        Raises
        ------
        BaseException
            The last exception raised if all retries are exhausted.
        """
        last_exception: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                return await coro_func(*args, **kwargs)
            except self.retry_on as exc:
                last_exception = exc

                if attempt < self.max_retries:
                    delay = _calculate_delay(
                        self.base_delay, attempt, self.max_delay, self.jitter
                    )
                    delay_ms = int(delay * 1000)

                    logger.warning(
                        f"Retry attempt {attempt + 1}/{self.max_retries} "
                        f"after {type(exc).__name__}: delaying {delay_ms}ms",
                        extra={
                            "attempt": attempt + 1,
                            "delay_ms": delay_ms,
                            "http_status": getattr(exc.response, "status_code", None)
                            if hasattr(exc, "response") and exc.response else None,
                        },
                    )

                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        f"Retry exhausted after {self.max_retries + 1} attempts: "
                        f"{type(exc).__name__}",
                        extra={
                            "attempt": attempt + 1,
                            "http_status": getattr(exc.response, "status_code", None)
                            if hasattr(exc, "response") and exc.response else None,
                        },
                    )

        raise last_exception  # type: ignore[misc]

    def __call__(self, func: F) -> F:
        """Decorator protocol: wrap an async function with retry logic.

        Parameters
        ----------
        func : Callable
            Async function to wrap.

        Returns
        -------
        Callable
            Wrapped async function with retry logic.
        """

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.execute(func, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    @asynccontextmanager
    async def context(self):
        """Async context manager protocol for retry handling.

        Usage
        -----
        >>> handler = RetryHandler()
        >>> async with handler.context() as h:
        ...     result = await h.execute(some_async_call)
        """
        yield self

    def __repr__(self) -> str:
        """Return string representation of RetryHandler configuration."""
        return (
            f"RetryHandler(max_retries={self.max_retries}, "
            f"base_delay={self.base_delay}, "
            f"max_delay={self.max_delay}, "
            f"jitter={self.jitter})"
        )
