"""Focused tests for B3 trace context validation and isolation."""

from __future__ import annotations

import asyncio
import contextvars
import sys
from pathlib import Path
from types import ModuleType

import pytest

SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pal_found_cli.common.tracing_provider import (
    B3Context,
    InvalidTraceContextError,
    TracingProvider,
)


@pytest.fixture
def sdk_context(monkeypatch: pytest.MonkeyPatch):
    module = ModuleType("foundry_sdk")
    module.TRACE_ID_VAR = contextvars.ContextVar("test_trace_id", default=None)
    module.SPAN_ID_VAR = contextvars.ContextVar("test_span_id", default=None)
    module.SAMPLED_VAR = contextvars.ContextVar("test_sampled", default=None)
    monkeypatch.setitem(sys.modules, "foundry_sdk", module)
    return module


def _context(seed: str, sampled: str = "1") -> B3Context:
    return B3Context(trace_id=seed * 32, span_id=seed * 16, sampled=sampled)


def test_disabled_scope_does_not_import_or_mutate_sdk_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "foundry_sdk", raising=False)

    with TracingProvider(enabled=False).scope() as context:
        assert context is None

    assert "foundry_sdk" not in sys.modules


def test_generated_context_has_valid_nonzero_b3_values_and_resets(sdk_context) -> None:
    provider = TracingProvider(enabled=True, sampled="0")

    with provider.scope() as context:
        assert context is not None
        assert len(context.trace_id) == 32
        assert len(context.span_id) == 16
        assert context.trace_id != "0" * 32
        assert context.span_id != "0" * 16
        assert set(context.trace_id) <= set("0123456789abcdef")
        assert set(context.span_id) <= set("0123456789abcdef")
        assert sdk_context.TRACE_ID_VAR.get() == context.trace_id
        assert sdk_context.SPAN_ID_VAR.get() == context.span_id
        assert sdk_context.SAMPLED_VAR.get() == "0"

    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None


@pytest.mark.parametrize(
    "context",
    [
        B3Context("0" * 32, "1" * 16, "1"),
        B3Context("A" * 32, "1" * 16, "1"),
        B3Context("1" * 31, "1" * 16, "1"),
        B3Context("1" * 32, "0" * 16, "1"),
        B3Context("1" * 32, "1" * 15, "1"),
        B3Context("1" * 32, "1" * 16, "yes"),
    ],
)
def test_invalid_supplied_context_is_rejected_before_sdk_mutation(
    sdk_context,
    context: B3Context,
) -> None:
    with pytest.raises(InvalidTraceContextError):
        with TracingProvider(enabled=True).scope(context):
            pytest.fail("invalid context entered scope")

    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None


def test_nested_scope_restores_outer_then_original_values(sdk_context) -> None:
    provider = TracingProvider(enabled=True)
    outer = _context("1")
    inner = _context("2", sampled="0")
    prior = sdk_context.TRACE_ID_VAR.set("f" * 32)
    try:
        with provider.scope(outer):
            assert sdk_context.TRACE_ID_VAR.get() == outer.trace_id
            with provider.scope(inner):
                assert sdk_context.TRACE_ID_VAR.get() == inner.trace_id
                assert sdk_context.SAMPLED_VAR.get() == "0"
            assert sdk_context.TRACE_ID_VAR.get() == outer.trace_id
            assert sdk_context.SAMPLED_VAR.get() == "1"
        assert sdk_context.TRACE_ID_VAR.get() == "f" * 32
        assert sdk_context.SPAN_ID_VAR.get() is None
        assert sdk_context.SAMPLED_VAR.get() is None
    finally:
        sdk_context.TRACE_ID_VAR.reset(prior)


def test_scope_resets_all_values_when_body_raises(sdk_context) -> None:
    context = _context("3")

    with pytest.raises(RuntimeError, match="operation failed"):
        with TracingProvider(enabled=True).scope(context):
            raise RuntimeError("operation failed")

    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_keep_distinct_contexts(sdk_context) -> None:
    provider = TracingProvider(enabled=True)
    barrier = asyncio.Event()
    entered = 0

    async def invoke(context: B3Context) -> tuple[str, str, str]:
        nonlocal entered
        with provider.scope(context):
            entered += 1
            if entered == 2:
                barrier.set()
            await barrier.wait()
            await asyncio.sleep(0)
            return (
                sdk_context.TRACE_ID_VAR.get(),
                sdk_context.SPAN_ID_VAR.get(),
                sdk_context.SAMPLED_VAR.get(),
            )

    first = _context("4", "0")
    second = _context("5", "1")
    observed = await asyncio.gather(invoke(first), invoke(second))

    assert observed == [
        (first.trace_id, first.span_id, first.sampled),
        (second.trace_id, second.span_id, second.sampled),
    ]
    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None


def test_back_to_back_scopes_do_not_reuse_context(sdk_context) -> None:
    provider = TracingProvider(enabled=True)
    with provider.scope() as first:
        assert first is not None
    with provider.scope() as second:
        assert second is not None

    assert first != second
    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None


@pytest.mark.asyncio
async def test_execute_traced_carries_same_b3_context_across_attempts_and_restores(
    sdk_context,
) -> None:
    """CODEREVIEW-005 D1: every retry attempt carries the same B3 context.

    DESIGN-005 §7 mandates an integration test proving that
    RetryHandler.execute_traced() runs all retry attempts under a single
    isolated B3 context. This wraps a coroutine that fails once then
    succeeds, records the SDK context vars on each attempt, and asserts
    they are identical across attempts and fully restored afterwards.
    """
    import requests

    from pal_found_cli.common.retry import RetryHandler

    handler = RetryHandler(
        max_retries=2,
        base_delay=0.001,
        jitter=False,
        timeout_s=None,
    )

    observed_contexts: list[tuple[str, str, str]] = []
    attempts = 0

    async def transient_then_ok() -> str:
        nonlocal attempts
        attempts += 1
        observed_contexts.append(
            (
                sdk_context.TRACE_ID_VAR.get(),
                sdk_context.SPAN_ID_VAR.get(),
                sdk_context.SAMPLED_VAR.get(),
            )
        )
        if attempts == 1:
            raise requests.RequestException("transient failure")
        return "ok"

    # Avoid real backoff delays inside the retry loop.
    real_sleep = asyncio.sleep

    async def instant_sleep(_delay: float) -> None:
        await real_sleep(0)

    asyncio.sleep = instant_sleep  # type: ignore[assignment]
    try:
        result = await handler.execute_traced(
            TracingProvider(enabled=True, sampled="1"),
            transient_then_ok,
        )
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]

    assert result == "ok"
    assert attempts == 2
    assert len(observed_contexts) == 2

    # Every attempt must run under the same B3 context values.
    first_attempt = observed_contexts[0]
    assert first_attempt[0] is not None, "first attempt had no trace_id"
    assert first_attempt[1] is not None, "first attempt had no span_id"
    assert first_attempt[2] == "1"
    assert observed_contexts[1] == first_attempt, (
        "retry attempt carried a different B3 context than the first attempt"
    )

    # All SDK context vars must be restored to their default values.
    assert sdk_context.TRACE_ID_VAR.get() is None
    assert sdk_context.SPAN_ID_VAR.get() is None
    assert sdk_context.SAMPLED_VAR.get() is None
