#!/usr/bin/env python3
"""RetryHandler with exponential backoff and jitter (ADR-002, FR-ERR-3/4).

Provides a configurable retry mechanism for async operations with
exponential backoff, optional jitter, and support for both decorator
and context manager protocols. Per-call timeouts are enforced via
``asyncio.wait_for()`` and SIGINT/SIGTERM cancellation is wired through
an optional signal handler (ADR-002 §Consequences).

Environment Variables (canonical, per SRS Table 5.3 / FR-ERR-4)
---------------------------------------------------------------
FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS : int
    Maximum number of retry attempts. Default: 4
FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS : float
    Base delay in **milliseconds** for exponential backoff. Default: 500
FOUNDRY_AGENTIC_CLI_RETRY_MAX_DELAY_MS : float
    Maximum delay cap in **milliseconds**. Default: 30000
FOUNDRY_AGENTIC_CLI_RETRY_MULTIPLIER : float
    Backoff multiplier applied to the previous delay. Default: 2.0
FOUNDRY_AGENTIC_CLI_RETRY_JITTER : bool
    Enable ±10% random jitter on delays. Default: true

Internal arithmetic keeps delays in seconds for ``asyncio.sleep()``; env
vars are read in milliseconds and converted at the boundary so the
canonical defaults match SRS Table 5.3.

Backoff Formula
---------------
delay_seconds = min(initial_ms * (multiplier ** attempt), max_ms) / 1000
With jitter: delay_seconds *= (1 + random.uniform(-0.1, 0.1))
"""

import asyncio
import logging
import os
import random
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

import requests

from foundry_cli.common.sdk_error_utils import (
    is_sdk_retryable_transport,
    sdk_http_status,
)

if TYPE_CHECKING:
    from foundry_cli.common.tracing_provider import B3Context, TracingProvider

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Canonical environment variable names (FR-ERR-4, SRS Table 5.3).
# Renamed from the legacy FOUNDRY_* prefix per architect decision on
# QUESTION-010 (2026-07-04): the canonical FOUNDRY_AGENTIC_CLI_*
# namespace is authoritative across all retry configuration.
ENV_MAX_ATTEMPTS = "FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS"
ENV_INITIAL_DELAY_MS = "FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS"
ENV_MAX_DELAY_MS = "FOUNDRY_AGENTIC_CLI_RETRY_MAX_DELAY_MS"
ENV_MULTIPLIER = "FOUNDRY_AGENTIC_CLI_RETRY_MULTIPLIER"
ENV_JITTER = "FOUNDRY_AGENTIC_CLI_RETRY_JITTER"

# Default values (milliseconds for delay vars, per SRS Table 5.3).
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_INITIAL_DELAY_MS = 500.0
DEFAULT_MAX_DELAY_MS = 30000.0
DEFAULT_MULTIPLIER = 2.0
DEFAULT_JITTER = True

# Per-call timeout (seconds). Read from the canonical env var so a single
# override lifts both RetryHandler default and any explicit wait_for site.
ENV_TIMEOUT_S = "FOUNDRY_AGENTIC_CLI_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 30.0

# HTTP status codes that MUST trigger retry per FR-ERR-3.
RETRYABLE_HTTP_STATUSES: set[int] = {429, 503}

# Legacy aliases retained for older tests/call sites. The canonical names above
# are authoritative; these constants are not read from the environment.
ENV_MAX_RETRIES = "FOUNDRY_MAX_RETRIES"
ENV_RETRY_BASE_DELAY = "FOUNDRY_RETRY_BASE_DELAY"
ENV_RETRY_MAX_DELAY = "FOUNDRY_RETRY_MAX_DELAY"
ENV_RETRY_JITTER = "FOUNDRY_RETRY_JITTER"
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0


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


def _is_retryable_http_status(exception: BaseException) -> bool:
    """Return True if *exception* carries an HTTP 429 or 503 response.

    Implements FR-ERR-3: only 429 (Too Many Requests) and 503 (Service
    Unavailable) are retryable. Other transport errors (connection reset,
    DNS failure, etc.) are also retryable via ``DEFAULT_RETRY_EXCEPTIONS``.

    Parameters
    ----------
    exception : BaseException
        The exception to inspect.

    Returns
    -------
    bool
        True if the exception exposes a 429/503 status code.
    """
    return sdk_http_status(exception) in RETRYABLE_HTTP_STATUSES


def _get_http_status(exception: BaseException) -> int | None:
    """Extract an HTTP status code from an exception response, if present."""
    return sdk_http_status(exception)


class SignalCancellationError(TimeoutError):
    """Raised when SIGINT or SIGTERM cancels an active retry operation."""


def _signal_name(signum: int) -> str:
    """Return a stable signal name for user-facing timeout errors."""
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


class _SignalCancellationScope:
    """Scoped SIGINT/SIGTERM handlers for one active async attempt."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop
        self.signum: int | None = None
        self._previous: dict[int, Any] = {}
        self._installed: list[tuple[int, str]] = []

    def install(self) -> Callable[[], None]:
        """Install handlers and return a restore callback."""
        target_loop = self.loop or asyncio.get_event_loop()
        if not target_loop.is_running():
            return lambda: None

        main_task = asyncio.current_task(target_loop)

        def _cancel(signum: int, frame: Any = None) -> None:
            self.signum = signum
            if main_task is not None and not main_task.done():
                target_loop.call_soon_threadsafe(main_task.cancel)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                target_loop.add_signal_handler(sig, _cancel, sig, None)
                self._installed.append((sig, "loop"))
            except (NotImplementedError, RuntimeError):
                try:
                    self._previous[sig] = signal.getsignal(sig)
                    signal.signal(sig, _cancel)
                    self._installed.append((sig, "signal"))
                except (ValueError, OSError):
                    pass

        return self.restore

    def restore(self) -> None:
        """Restore signal handlers registered by this scope."""
        target_loop = self.loop or asyncio.get_event_loop()
        for sig, mechanism in self._installed:
            if mechanism == "loop":
                try:
                    target_loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass
            else:
                try:
                    signal.signal(sig, self._previous[sig])
                except (KeyError, ValueError, OSError):
                    pass
        self._installed.clear()
        self._previous.clear()


# Default retryable exception types. Broad transport errors are kept as the
# outer net, but HTTP-status-aware retrying is layered on top via
# ``_should_retry`` which inspects 429/503 first (FR-ERR-3, BUG-SUB-002).
DEFAULT_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.RequestException,
    requests.ConnectionError,
    asyncio.TimeoutError,
)


def _calculate_delay(
    initial_delay_ms: float,
    attempt: int,
    max_delay_ms: float,
    multiplier: float,
    jitter: bool,
) -> float:
    """Calculate retry delay (returned in **seconds**) with backoff and jitter.

    Formula::

        delay_ms = min(initial_ms * (multiplier ** attempt), max_ms)
        delay_s  = delay_ms / 1000
        with jitter: delay_s *= (1 + random.uniform(-0.1, 0.1))

    Parameters
    ----------
    initial_delay_ms : float
        Base delay in milliseconds.
    attempt : int
        Current attempt number (0-indexed).
    max_delay_ms : float
        Maximum delay cap in milliseconds.
    multiplier : float
        Backoff multiplier.
    jitter : bool
        Whether to apply random jitter.

    Returns
    -------
    float
        Calculated delay in **seconds** (ready for ``asyncio.sleep``).
    """
    delay_ms = min(initial_delay_ms * (multiplier**attempt), max_delay_ms)
    delay_s = delay_ms / 1000.0

    if jitter:
        jitter_factor = 1.0 + random.uniform(-0.1, 0.1)  # nosec B311 - retry jitter is not security-sensitive.
        delay_s = delay_s * jitter_factor

    return delay_s


class RetryHandler:
    """Configurable retry handler with exponential backoff and jitter.

    Supports both decorator and async context manager protocols, enforces
    per-call timeouts via ``asyncio.wait_for()`` (ADR-002, BUG-SUB-001),
    and treats HTTP 429/503 as retryable per FR-ERR-3 (BUG-SUB-002).
    Logs retry attempts to stderr via NDJSON structured logging (ADR-005).

    Parameters
    ----------
    max_retries : int, optional
        Maximum number of retry attempts. Read from env var if not set.
    base_delay : float, optional
        Base delay in **milliseconds** for backoff. Read from env var if
        not set. Kept under the legacy name for call-site compatibility;
        internally converted to seconds before sleeping.
    max_delay : float, optional
        Maximum delay cap in **milliseconds**. Read from env var if not set.
    multiplier : float, optional
        Backoff multiplier. Read from env var if not set.
    jitter : bool, optional
        Enable ±10% random jitter. Read from env var if not set.
    retry_on : tuple of Exception types, optional
        Exception types to retry on. Defaults to HTTP-related exceptions.
    timeout_s : float, optional
        Per-call timeout in seconds. ``None`` disables the timeout wrapper.
        Read from env var if not set.

    Examples
    --------
    As decorator::

        >>> handler = RetryHandler(max_retries=3)
        >>> @handler
        ... async def call_api():
        ...     return await session.get(url)

    As context manager::

        >>> handler = RetryHandler()
        >>> async with handler as h:
        ...     result = await h.execute(session.get(url))
    """

    def __init__(
        self,
        max_retries: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        multiplier: float | None = None,
        jitter: bool | None = None,
        retry_on: tuple[type[BaseException], ...] | None = None,
        timeout_s: float | None = ...,  # type: ignore[assignment]
    ) -> None:
        # ``max_retries`` is accepted for call-site backwards compatibility;
        # canonical name going forward is "max_attempts".
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.environ.get(ENV_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS))
        )
        # Delays are read from canonical *_MS env vars. Accept explicit
        # constructor overrides in **milliseconds** to match the env contract.
        self.base_delay_ms = (
            base_delay
            if base_delay is not None
            else float(os.environ.get(ENV_INITIAL_DELAY_MS, DEFAULT_INITIAL_DELAY_MS))
        )
        self.max_delay_ms = (
            max_delay
            if max_delay is not None
            else float(os.environ.get(ENV_MAX_DELAY_MS, DEFAULT_MAX_DELAY_MS))
        )
        self.multiplier = (
            multiplier
            if multiplier is not None
            else float(os.environ.get(ENV_MULTIPLIER, DEFAULT_MULTIPLIER))
        )
        self.jitter = (
            jitter
            if jitter is not None
            else _parse_bool_env(ENV_JITTER, DEFAULT_JITTER)
        )
        self.retry_on = retry_on if retry_on is not None else DEFAULT_RETRY_EXCEPTIONS
        # Sentinel default: when caller passes nothing, read env var or fall
        # back to the canonical timeout. ``None`` explicitly disables the
        # wait_for wrapper (used by unit tests that want raw control).
        if timeout_s is ...:
            timeout_s = float(os.environ.get(ENV_TIMEOUT_S, DEFAULT_TIMEOUT_S))
        self.timeout_s: float | None = timeout_s

    # Backwards-compatible alias: ``base_delay``/``max_delay`` historically
    # returned seconds. To preserve existing call sites and repr output we
    # expose the values in **seconds** via these properties while keeping the
    # canonical-ms value as the source of truth internally.
    @property
    def base_delay(self) -> float:
        """Base delay in seconds (derived from ``base_delay_ms``)."""
        return self.base_delay_ms / 1000.0

    @property
    def max_delay(self) -> float:
        """Maximum delay cap in seconds (derived from ``max_delay_ms``)."""
        return self.max_delay_ms / 1000.0

    def _should_retry(self, exception: BaseException) -> bool:
        """Check if an exception should trigger a retry.

        Logic:
        1. Signal-triggered cancellation never retries; it exits as timeout.
        2. If an HTTP status is present, retry only 429/503.
        3. If no HTTP status is present, retry configured exception types.

        Parameters
        ----------
        exception : BaseException
            The exception to check.

        Returns
        -------
        bool
            True if the exception is retryable.
        """
        if isinstance(exception, SignalCancellationError):
            return False

        status = _get_http_status(exception)
        if status is not None:
            return status in RETRYABLE_HTTP_STATUSES

        return is_sdk_retryable_transport(exception) or isinstance(
            exception, self.retry_on
        ) or isinstance(
            exception, asyncio.TimeoutError
        )

    def _execute_with_timeout(
        self,
        coro_func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Build one awaitable attempt, applying the configured timeout."""
        coroutine = coro_func(*args, **kwargs)
        if self.timeout_s is None:
            return coroutine
        return asyncio.wait_for(coroutine, timeout=self.timeout_s)

    async def execute(
        self,
        coro_func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute an async callable with retry logic and per-call timeout.

        Wraps each attempt in ``asyncio.wait_for()`` so that hung
        transports cannot stall the retry loop (BUG-SUB-001). Timeouts
        raised by ``wait_for`` surface as ``asyncio.TimeoutError`` and
        are retried like any other configured exception.

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
        last_exception: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            signal_scope = _SignalCancellationScope()
            restore_signals = signal_scope.install()
            try:
                try:
                    return await self._execute_with_timeout(coro_func, *args, **kwargs)
                except asyncio.CancelledError as exc:
                    if signal_scope.signum is not None:
                        signal_name = _signal_name(signal_scope.signum)
                        raise SignalCancellationError(
                            f"Operation cancelled by {signal_name}"
                        ) from exc
                    raise
            except BaseException as exc:
                if not self._should_retry(exc):
                    raise

                last_exception = exc

                if attempt < self.max_retries:
                    delay_s = _calculate_delay(
                        self.base_delay_ms,
                        attempt,
                        self.max_delay_ms,
                        self.multiplier,
                        self.jitter,
                    )
                    delay_ms = int(delay_s * 1000)

                    logger.warning(
                        f"Retry attempt {attempt + 1}/{self.max_retries} "
                        f"after {type(exc).__name__}: delaying {delay_ms}ms",
                        extra={
                            "attempt": attempt + 1,
                            "delay_ms": delay_ms,
                            "http_status": _get_http_status(exc),
                        },
                    )

                    await asyncio.sleep(delay_s)
                else:
                    logger.error(
                        f"Retry exhausted after {self.max_retries + 1} attempts: "
                        f"{type(exc).__name__}",
                        extra={
                            "attempt": attempt + 1,
                            "http_status": _get_http_status(exc),
                        },
                    )
            finally:
                restore_signals()

        raise last_exception  # type: ignore[misc]

    async def execute_traced(
        self,
        tracing: "TracingProvider",
        coro_func: Callable[..., Any],
        *args: Any,
        supplied: "B3Context | None" = None,
        **kwargs: Any,
    ) -> Any:
        """Execute every retry attempt under one isolated B3 context."""
        with tracing.scope(supplied):
            return await self.execute(coro_func, *args, **kwargs)

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

        Usage::

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
            f"multiplier={self.multiplier}, "
            f"jitter={self.jitter})"
        )


def install_signal_cancellation(
    loop: asyncio.AbstractEventLoop | None = None,
) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers that cancel the running task.

    Implements ADR-002 §Consequences: SIGINT/SIGTERM must cancel the
    ``asyncio.wait_for`` wait. The handler cancels the current task so
    that ``asyncio.TimeoutError``/``CancelledError`` propagates to the
    retry loop and exits to ``ErrorSerializer`` which maps it to exit
    code 5 (TimeoutError per ADR-001).

    Parameters
    ----------
    loop : AbstractEventLoop, optional
        Event loop to register handlers against. Defaults to the running
        loop if one exists.

    Returns
    -------
    Callable[[], None]
        Restore function that removes the installed handlers. Call it in
        a ``finally:`` block to avoid leaking handlers across invocations.
    """
    target_loop = loop or asyncio.get_event_loop()
    if not target_loop.is_running():
        # Nothing to cancel yet; callers should install during the run.
        return lambda: None

    main_task = asyncio.current_task(target_loop)

    def _cancel(signum: int, frame: Any = None) -> None:
        if main_task is not None and not main_task.done():
            target_loop.call_soon_threadsafe(main_task.cancel)

    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            target_loop.add_signal_handler(sig, _cancel, sig, None)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            # add_signal_handler is unavailable on Windows / non-main thread.
            # Fall back to the regular signal API where possible.
            try:
                previous = signal.getsignal(sig)
                signal.signal(sig, _cancel)
                installed.append(sig)
                # Stash previous so we can restore.
                _PREV_SIGNALS[sig] = previous  # type: ignore[assignment]
            except (ValueError, OSError):
                # Giving up silently: signal installation is best-effort.
                pass

    def _restore() -> None:
        for sig in installed:
            try:
                target_loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                try:
                    prev = _PREV_SIGNALS.pop(sig, None)
                    if prev is not None:
                        signal.signal(sig, prev)
                except (ValueError, OSError):
                    pass

    return _restore


# Stores the previous signal handlers so we can restore them on cleanup.
_PREV_SIGNALS: dict = {}
