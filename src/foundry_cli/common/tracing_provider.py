"""SDK-native B3 trace context generation and isolation."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from foundry_cli.common.config_loader import ConfigLoader

_LOWER_HEX = frozenset("0123456789abcdef")


class InvalidTraceContextError(ValueError):
    """Raised when caller-supplied B3 context is malformed."""

    exit_code = 1


@dataclass(frozen=True)
class B3Context:
    """Validated B3 multi-header values used by the Foundry SDK."""

    trace_id: str
    span_id: str
    sampled: str


class TracingProvider:
    """Create isolated Foundry SDK B3 contexts for one invocation."""

    def __init__(
        self,
        enabled: bool | None = None,
        *,
        config: ConfigLoader | None = None,
        sampled: str = "1",
    ) -> None:
        cfg = config or ConfigLoader()
        self.enabled = cfg.enable_tracing if enabled is None else enabled
        if sampled not in {"0", "1"}:
            raise InvalidTraceContextError("B3 sampled value must be '0' or '1'")
        self.sampled = sampled

    @contextmanager
    def scope(self, supplied: B3Context | None = None) -> Iterator[B3Context | None]:
        """Set SDK context variables and restore prior values on every exit."""
        if not self.enabled:
            yield None
            return

        context = supplied or B3Context(
            trace_id=self._new_nonzero_hex(16),
            span_id=self._new_nonzero_hex(8),
            sampled=self.sampled,
        )
        self.validate(context)

        try:
            from foundry_sdk import SAMPLED_VAR, SPAN_ID_VAR, TRACE_ID_VAR
        except ImportError as exc:
            from foundry_cli.common.config_loader import ConfigurationError

            raise ConfigurationError(
                "foundry-sdk not installed; tracing context is unavailable"
            ) from exc

        trace_token = TRACE_ID_VAR.set(context.trace_id)
        span_token = SPAN_ID_VAR.set(context.span_id)
        sampled_token = SAMPLED_VAR.set(context.sampled)
        try:
            yield context
        finally:
            SAMPLED_VAR.reset(sampled_token)
            SPAN_ID_VAR.reset(span_token)
            TRACE_ID_VAR.reset(trace_token)

    @staticmethod
    def validate(context: B3Context) -> None:
        """Validate lowercase, nonzero 128-bit/64-bit B3 identifiers."""
        if not TracingProvider._valid_hex(context.trace_id, 32):
            raise InvalidTraceContextError(
                "B3 trace_id must be 32 lowercase hexadecimal characters and nonzero"
            )
        if not TracingProvider._valid_hex(context.span_id, 16):
            raise InvalidTraceContextError(
                "B3 span_id must be 16 lowercase hexadecimal characters and nonzero"
            )
        if context.sampled not in {"0", "1"}:
            raise InvalidTraceContextError("B3 sampled value must be '0' or '1'")

    @staticmethod
    def _valid_hex(value: str, length: int) -> bool:
        return (
            isinstance(value, str)
            and len(value) == length
            and value != "0" * length
            and all(character in _LOWER_HEX for character in value)
        )

    @staticmethod
    def _new_nonzero_hex(byte_count: int) -> str:
        value = "0" * (byte_count * 2)
        while value == "0" * (byte_count * 2):
            value = secrets.token_hex(byte_count)
        return value
