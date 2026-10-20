"""Behavioral oracles for QUESTION-192 timeout and total-call decisions."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import requests

from pal_found_cli.common.config_loader import ConfigLoader
from pal_found_cli.common.retry import ENV_MAX_ATTEMPTS, RetryHandler
from pal_found_cli.common.timeout_config import ENV_TIMEOUT_S, resolve_timeout


@pytest.fixture(autouse=True)
def clean_contract_env(monkeypatch):
    monkeypatch.delenv(ENV_MAX_ATTEMPTS, raising=False)
    monkeypatch.delenv(ENV_TIMEOUT_S, raising=False)
    monkeypatch.delenv("FOUNDRY_AGENTIC_CLI_STREAMS_TIMEOUT_S", raising=False)


@pytest.mark.parametrize(
    "raw, expected",
    [(None, 30), ("1", 1), ("3600", 3600), ("0", 1), ("-5", 1),
     ("5000", 3600), ("bad", 30), ("1.5", 30), ("nan", 30), ("inf", 30)],
)
def test_environment_timeout_shared_by_config_and_retry(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv(ENV_TIMEOUT_S, raw)
    assert resolve_timeout() == expected
    assert ConfigLoader().timeout_s == expected
    assert RetryHandler().timeout_s == expected


@pytest.mark.parametrize("value", [0, -1, 3601, 1.5, "3", True, float("nan"), float("inf")])
def test_cli_timeout_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="--timeout"):
        resolve_timeout(value)


@pytest.mark.parametrize("value", [1, 120, 3600])
def test_cli_timeout_overrides_lower_environment(monkeypatch, value):
    monkeypatch.setenv(ENV_TIMEOUT_S, "1")
    assert resolve_timeout(value) == value


@pytest.mark.parametrize("raw, expected", [(None, 120), ("bad", 120), ("0", 1), ("5000", 3600)])
def test_streams_timeout_preserves_separate_namespace_default(monkeypatch, raw, expected):
    monkeypatch.setenv(ENV_TIMEOUT_S, "5")
    name = "FOUNDRY_AGENTIC_CLI_STREAMS_TIMEOUT_S"
    if raw is not None:
        monkeypatch.setenv(name, raw)
    assert resolve_timeout(env_var=name, default=120) == expected
    assert resolve_timeout(200, env_var=name, default=120) == 200


@pytest.mark.parametrize("value", [0, -1, 3601, float("nan"), float("inf"), True, "30"])
def test_internal_timeout_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="timeout_s"):
        RetryHandler(timeout_s=value)


@pytest.mark.asyncio
async def test_wait_for_receives_resolved_timeout(monkeypatch):
    monkeypatch.setenv(ENV_TIMEOUT_S, "1")
    timeout = resolve_timeout(120)
    recorded = []

    async def capture(coroutine, *, timeout):
        recorded.append(timeout)
        return await coroutine

    monkeypatch.setattr("pal_found_cli.common.retry.asyncio.wait_for", capture)
    result = await RetryHandler(timeout_s=timeout).execute(AsyncMock(return_value="ok"))
    assert result == "ok"
    assert recorded == [120]


@pytest.mark.asyncio
async def test_timeout_cancels_blocked_attempt_and_maps_to_exit_five():
    from pal_found_cli.common.error_serializer import ErrorSerializer

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError) as caught:
        await RetryHandler(max_attempts=1, timeout_s=0.01).execute(blocked)
    assert entered.is_set() and cancelled.is_set()
    assert ErrorSerializer().serialize(caught.value, print_to_stdout=False) == 5


@pytest.mark.asyncio
async def test_internal_none_bypass_and_success_before_deadline():
    call = AsyncMock(return_value="ok")
    assert await RetryHandler(timeout_s=None).execute(call) == "ok"
    assert await RetryHandler(timeout_s=1).execute(call) == "ok"


@pytest.mark.parametrize(
    "raw, calls", [(None, 4), ("1", 1), ("3", 3), ("0", 1), ("-5", 1),
                   ("bad", 4), ("1.5", 4), ("nan", 4), ("inf", 4)],
)
@pytest.mark.asyncio
async def test_canonical_attempts_count_actual_calls_and_sleeps(monkeypatch, raw, calls):
    if raw is not None:
        monkeypatch.setenv(ENV_MAX_ATTEMPTS, raw)
    sleep = AsyncMock()
    monkeypatch.setattr("pal_found_cli.common.retry.asyncio.sleep", sleep)
    error = requests.ConnectionError("offline fixture")
    call = AsyncMock(side_effect=error)
    with pytest.raises(requests.ConnectionError) as caught:
        await RetryHandler(timeout_s=None).execute(call)
    assert caught.value is error
    assert call.await_count == calls
    assert sleep.await_count == calls - 1


@pytest.mark.parametrize("legacy, calls", [(0, 1), (2, 3)])
@pytest.mark.asyncio
async def test_explicit_legacy_retry_count_is_preserved(monkeypatch, legacy, calls):
    sleep = AsyncMock()
    monkeypatch.setattr("pal_found_cli.common.retry.asyncio.sleep", sleep)
    call = AsyncMock(side_effect=requests.ConnectionError())
    with pytest.raises(requests.ConnectionError):
        await RetryHandler(max_retries=legacy, timeout_s=None).execute(call)
    assert call.await_count == calls
    assert sleep.await_count == legacy


@pytest.mark.parametrize("value", [0, -1, 1.5, "4", True])
def test_explicit_attempt_count_requires_positive_integer(value):
    with pytest.raises(ValueError, match="max_attempts"):
        RetryHandler(max_attempts=value)


@pytest.mark.parametrize("value", [-1, 1.5, "4", True])
def test_explicit_legacy_count_requires_nonnegative_integer(value):
    with pytest.raises(ValueError, match="max_retries"):
        RetryHandler(max_retries=value)


def test_conflicting_count_keywords_rejected_and_no_environment_cap(monkeypatch):
    with pytest.raises(ValueError, match="not both"):
        RetryHandler(max_attempts=4, max_retries=0)
    monkeypatch.setenv(ENV_MAX_ATTEMPTS, "100000")
    assert RetryHandler().max_attempts == 100000


@pytest.mark.asyncio
async def test_success_stops_retries_and_nonretryable_failure_runs_once(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("pal_found_cli.common.retry.asyncio.sleep", sleep)
    call = AsyncMock(side_effect=[requests.ConnectionError(), "ok"])
    assert await RetryHandler(timeout_s=None).execute(call) == "ok"
    assert call.await_count == 2 and sleep.await_count == 1
    sleep.reset_mock()
    bad = AsyncMock(side_effect=ValueError("input"))
    with pytest.raises(ValueError):
        await RetryHandler(timeout_s=None).execute(bad)
    assert bad.await_count == 1 and sleep.await_count == 0
