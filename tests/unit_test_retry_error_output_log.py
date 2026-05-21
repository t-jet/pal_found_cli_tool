#!/usr/bin/env python3
"""Unit tests for RetryHandler, ErrorSerializer, OutputFormatter, LogSetup (UNITTEST-002).

Pure unit tests — all external dependencies mocked at the module level.
Covers main scenarios and edge cases for each component as defined in DEV-STORY-002.

Framework: pytest + pytest-asyncio
Run: pytest tests/unit_test_retry_error_output_log.py -v --tb=long
"""

import asyncio
import json
import logging
import math
import pathlib
import sys
import tempfile
from functools import wraps
from io import StringIO
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src is on path
_SRC_PATH = pathlib.Path(__file__).parent.parent / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def reset_log_setup():
    """Reset LogSetup singleton before/after each test."""
    from foundry_cli.common.log_setup import LogSetup
    LogSetup.reset()
    yield
    LogSetup.reset()


@pytest.fixture
def clean_retry_env(monkeypatch):
    """Strip all retry-related env vars."""
    keys = [
        "FOUNDRY_MAX_RETRIES",
        "FOUNDRY_RETRY_BASE_DELAY",
        "FOUNDRY_RETRY_MAX_DELAY",
        "FOUNDRY_RETRY_JITTER",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def clean_output_env(monkeypatch):
    """Strip output format env var."""
    monkeypatch.delenv("FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT", raising=False)
    return monkeypatch


@pytest.fixture
def clean_log_env(monkeypatch):
    """Strip log-related env vars."""
    monkeypatch.delenv("FOUNDRY_AGENTIC_CLI_LOG_LEVEL", raising=False)
    return monkeypatch


# ===========================================================================
# Helpers
# ===========================================================================

def _mock_http_exception(status_code):
    """Create a mock exception with an HTTP response status code."""
    exc: Any = Exception(f"HTTP {status_code}")
    exc.response = MagicMock()
    exc.response.status_code = status_code
    return exc


# ===========================================================================
# RetryHandler — Unit Tests
# ===========================================================================

class TestCalculateDelay:
    """Test _calculate_delay standalone function."""

    def test_delay_attempt_zero(self):
        """delay at attempt 0 = base_delay (no backoff yet)."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=1.0, attempt=0, max_delay=30.0, jitter=False)
        assert delay == 1.0

    def test_delay_attempt_one_doubles(self):
        """delay at attempt 1 = base_delay * 2."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=1.0, attempt=1, max_delay=30.0, jitter=False)
        assert delay == 2.0

    def test_delay_attempt_two_quadruples(self):
        """delay at attempt 2 = base_delay * 4."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=1.0, attempt=2, max_delay=30.0, jitter=False)
        assert delay == 4.0

    def test_delay_capped_at_max_delay(self):
        """delay capped at max_delay regardless of attempt."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=10.0, attempt=5, max_delay=30.0, jitter=False)
        # 10 * 2^5 = 320, should be capped at 30
        assert delay == 30.0

    def test_delay_respects_max_delay_boundary(self):
        """delay exactly at max_delay boundary."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=15.0, attempt=1, max_delay=30.0, jitter=False)
        # 15 * 2 = 30, exactly at cap
        assert delay == 30.0

    def test_jitter_enabled_varies_delay(self):
        """Jitter should produce delay within ±10% of base."""
        from foundry_cli.common.retry import _calculate_delay
        delays = [
            _calculate_delay(base_delay=10.0, attempt=0, max_delay=30.0, jitter=True)
            for _ in range(50)
        ]
        # All delays should be between 9.0 and 11.0 (10 ± 10%)
        assert all(9.0 <= d <= 11.0 for d in delays)

    def test_jitter_disabled_deterministic(self):
        """Without jitter, delay is deterministic."""
        from foundry_cli.common.retry import _calculate_delay
        d1 = _calculate_delay(base_delay=1.0, attempt=3, max_delay=30.0, jitter=False)
        d2 = _calculate_delay(base_delay=1.0, attempt=3, max_delay=30.0, jitter=False)
        assert d1 == d2

    def test_zero_base_delay(self):
        """Zero base_delay produces zero delay."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=0.0, attempt=5, max_delay=30.0, jitter=False)
        assert delay == 0.0

    def test_negative_base_delay_clamped_by_formula(self):
        """Negative base_delay produces negative delay (edge case)."""
        from foundry_cli.common.retry import _calculate_delay
        delay = _calculate_delay(base_delay=-1.0, attempt=0, max_delay=30.0, jitter=False)
        assert delay == -1.0


class TestParseBoolEnv:
    """Test _parse_bool_env helper."""

    def test_default_when_not_set(self, clean_retry_env):
        from foundry_cli.common.retry import _parse_bool_env
        result = _parse_bool_env("NONEXISTENT_VAR", True)
        assert result is True

    def test_true_values(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import _parse_bool_env
        for val in ("true", "1", "yes", "on"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _parse_bool_env("TEST_BOOL", False) is True

    def test_false_values(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import _parse_bool_env
        for val in ("false", "0", "no", "off", "random"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _parse_bool_env("TEST_BOOL", True) is False

    def test_case_insensitive(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import _parse_bool_env
        monkeypatch.setenv("TEST_BOOL", "TRUE")
        assert _parse_bool_env("TEST_BOOL", False) is True


class TestRetryHandlerInit:
    """Test RetryHandler initialization and defaults."""

    def test_defaults(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler, DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY, DEFAULT_MAX_DELAY, DEFAULT_JITTER
        handler = RetryHandler()
        assert handler.max_retries == DEFAULT_MAX_RETRIES
        assert handler.base_delay == DEFAULT_BASE_DELAY
        assert handler.max_delay == DEFAULT_MAX_DELAY
        assert handler.jitter == DEFAULT_JITTER

    def test_explicit_params_override_defaults(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=5, base_delay=2.0, max_delay=60.0, jitter=False)
        assert handler.max_retries == 5
        assert handler.base_delay == 2.0
        assert handler.max_delay == 60.0
        assert handler.jitter is False

    def test_env_var_max_retries(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import RetryHandler, ENV_MAX_RETRIES
        monkeypatch.setenv(ENV_MAX_RETRIES, "7")
        handler = RetryHandler()
        assert handler.max_retries == 7

    def test_env_var_base_delay(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import RetryHandler, ENV_RETRY_BASE_DELAY
        monkeypatch.setenv(ENV_RETRY_BASE_DELAY, "5.5")
        handler = RetryHandler()
        assert handler.base_delay == 5.5

    def test_env_var_max_delay(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import RetryHandler, ENV_RETRY_MAX_DELAY
        monkeypatch.setenv(ENV_RETRY_MAX_DELAY, "100.0")
        handler = RetryHandler()
        assert handler.max_delay == 100.0

    def test_env_var_jitter(self, clean_retry_env, monkeypatch):
        from foundry_cli.common.retry import RetryHandler, ENV_RETRY_JITTER
        monkeypatch.setenv(ENV_RETRY_JITTER, "false")
        handler = RetryHandler()
        assert handler.jitter is False

    def test_explicit_params_override_env(self, clean_retry_env, monkeypatch):
        """Explicit constructor params take precedence over env vars."""
        from foundry_cli.common.retry import RetryHandler, ENV_MAX_RETRIES
        monkeypatch.setenv(ENV_MAX_RETRIES, "7")
        handler = RetryHandler(max_retries=2)
        assert handler.max_retries == 2

    def test_retry_on_default(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler, DEFAULT_RETRY_EXCEPTIONS
        handler = RetryHandler()
        assert handler.retry_on == DEFAULT_RETRY_EXCEPTIONS

    def test_retry_on_custom(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(retry_on=(ValueError, TypeError))
        assert handler.retry_on == (ValueError, TypeError)

    def test_repr(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3, base_delay=1.0, max_delay=30.0, jitter=True)
        r = repr(handler)
        assert "max_retries=3" in r
        assert "base_delay=1.0" in r
        assert "max_delay=30.0" in r
        assert "jitter=True" in r


class TestRetryHandlerShouldRetry:
    """Test _should_retry method."""

    def test_retry_on_matching_exception(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(retry_on=(ValueError,))
        assert handler._should_retry(ValueError("test")) is True

    def test_no_retry_on_unmatched_exception(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(retry_on=(ValueError,))
        assert handler._should_retry(TypeError("test")) is False

    def test_retry_on_subclass(self, clean_retry_env):
        from foundry_cli.common.retry import RetryHandler
        class CustomError(ValueError):
            pass
        handler = RetryHandler(retry_on=(ValueError,))
        assert handler._should_retry(CustomError("test")) is True


class TestRetryHandlerExecute:
    """Test execute() method with async callable."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, clean_retry_env):
        """Successful call returns immediately without retry."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3)
        mock_coro = AsyncMock(return_value="success")
        result = await handler.execute(mock_coro)
        assert result == "success"
        mock_coro.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_after_one_retry(self, clean_retry_env):
        """One failure then success → 2 calls total."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3, base_delay=0.0, jitter=False, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=[ValueError("fail"), "success"])
        result = await handler.execute(mock_coro)
        assert result == "success"
        assert mock_coro.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_exhaustion_raises(self, clean_retry_env):
        """All retries exhausted → raises last exception."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=2, base_delay=0.0, jitter=False, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=ValueError("always fails"))
        with pytest.raises(ValueError, match="always fails"):
            await handler.execute(mock_coro)
        assert mock_coro.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_non_retryable_exception_no_retry(self, clean_retry_env):
        """Exception not in retry_on → raises immediately."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=TypeError("not retryable"))
        with pytest.raises(TypeError, match="not retryable"):
            await handler.execute(mock_coro)
        mock_coro.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_retries_still_one_attempt(self, clean_retry_env):
        """max_retries=0 → one attempt, no retries."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=0, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=ValueError("fail"))
        with pytest.raises(ValueError, match="fail"):
            await handler.execute(mock_coro)
        mock_coro.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_passes_args_kwargs(self, clean_retry_env):
        """execute forwards args and kwargs to the callable."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=0)
        mock_coro = AsyncMock(return_value="ok")
        result = await handler.execute(mock_coro, "arg1", kw="kw1")
        mock_coro.assert_called_once_with("arg1", kw="kw1")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_sleep_called_with_delay(self, clean_retry_env):
        """Verify asyncio.sleep is called with calculated delay."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=1, base_delay=0.5, jitter=False, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=ValueError("fail"))
        with patch("foundry_cli.common.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ValueError):
                await handler.execute(mock_coro)
            mock_sleep.assert_called_once_with(0.5)

    @pytest.mark.asyncio
    async def test_retry_with_different_delays(self, clean_retry_env):
        """Exponential backoff: each retry has different delay."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3, base_delay=1.0, jitter=False, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=ValueError("fail"))
        with patch("foundry_cli.common.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ValueError):
                await handler.execute(mock_coro)
            calls = [c[0][0] for c in mock_sleep.call_args_list]
            assert calls == [1.0, 2.0, 4.0]

    @pytest.mark.asyncio
    async def test_delay_capped_in_execute(self, clean_retry_env):
        """Delay capped at max_delay during actual execution."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=3, base_delay=10.0, max_delay=15.0, jitter=False, retry_on=(ValueError,))
        mock_coro = AsyncMock(side_effect=ValueError("fail"))
        with patch("foundry_cli.common.retry.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(ValueError):
                await handler.execute(mock_coro)
            calls = [c[0][0] for c in mock_sleep.call_args_list]
            assert all(d <= 15.0 for d in calls)
            assert calls[0] == 10.0  # 10 * 2^0 = 10
            assert calls[1] == 15.0  # 10 * 2^1 = 20 → capped at 15


class TestRetryHandlerDecorator:
    """Test __call__ decorator protocol."""

    @pytest.mark.asyncio
    async def test_decorator_wraps_function(self, clean_retry_env):
        """Decorator wraps async function and retries on failure."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=2, base_delay=0.0, jitter=False, retry_on=(ValueError,))

        call_count = 0
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError("fail")
            return "success"

        wrapped = handler(flaky)
        result = await wrapped()
        assert result == "success"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_decorator_preserves_name(self, clean_retry_env):
        """@wraps preserves function name."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler()

        async def my_func():
            return "ok"

        wrapped = handler(my_func)
        assert wrapped.__name__ == "my_func"

    @pytest.mark.asyncio
    async def test_decorator_preserves_docstring(self, clean_retry_env):
        """@wraps preserves docstring."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler()

        async def documented_func():
            """This is a docstring."""
            return "ok"

        wrapped = handler(documented_func)
        assert wrapped.__doc__ == "This is a docstring."

    @pytest.mark.asyncio
    async def test_decorator_passes_args(self, clean_retry_env):
        """Decorator forwards args and kwargs."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=0)

        async def add(a, b):
            return a + b

        wrapped = handler(add)
        result = await wrapped(3, 4)
        assert result == 7


class TestRetryHandlerContextManager:
    """Test async context manager protocol."""

    @pytest.mark.asyncio
    async def test_context_manager_yields_self(self, clean_retry_env):
        """Context manager yields the handler instance."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=0)
        async with handler.context() as ctx:
            assert ctx is handler

    @pytest.mark.asyncio
    async def test_context_manager_execute(self, clean_retry_env):
        """Can call execute within context manager."""
        from foundry_cli.common.retry import RetryHandler
        handler = RetryHandler(max_retries=0)
        mock_coro = AsyncMock(return_value="ctx_result")
        async with handler.context() as ctx:
            result = await ctx.execute(mock_coro)
        assert result == "ctx_result"


# ===========================================================================
# ErrorSerializer — Unit Tests
# ===========================================================================

class TestErrorSerializerInit:
    """Test ErrorSerializer initialization."""

    def test_auto_generates_call_id(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer()
        # UUID format check
        assert len(ser.call_id) == 36
        assert ser.call_id[8] == "-"

    def test_custom_call_id(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer(call_id="custom-123")
        assert ser.call_id == "custom-123"


class TestErrorSerializerExitCodeMapping:
    """Test exception-to-exit-code mappings per ADR-001."""

    def test_auth_error_exit_code_2(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, _SDKAuthError, EXIT_AUTH
        ser = ErrorSerializer()
        code = ser.serialize(_SDKAuthError("unauthorized"), print_to_stdout=False)
        assert code == EXIT_AUTH

    def test_validation_error_exit_code_1(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, _SDKValidationError, EXIT_USER_INPUT
        ser = ErrorSerializer()
        code = ser.serialize(_SDKValidationError("invalid input"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_value_error_exit_code_1(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_USER_INPUT
        ser = ErrorSerializer()
        code = ser.serialize(ValueError("bad value"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_type_error_exit_code_1(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_USER_INPUT
        ser = ErrorSerializer()
        code = ser.serialize(TypeError("wrong type"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_permission_error_exit_code_3(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_PERMISSION_DENIED
        ser = ErrorSerializer()
        code = ser.serialize(PermissionError("forbidden"), print_to_stdout=False)
        assert code == EXIT_PERMISSION_DENIED

    def test_not_found_error_exit_code_4(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, _SDKNotFoundError, EXIT_NOT_FOUND
        ser = ErrorSerializer()
        code = ser.serialize(_SDKNotFoundError("missing"), print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

    def test_file_not_found_exit_code_4(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_NOT_FOUND
        ser = ErrorSerializer()
        code = ser.serialize(FileNotFoundError("no file"), print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

    def test_timeout_error_exit_code_5(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_TIMEOUT
        ser = ErrorSerializer()
        code = ser.serialize(TimeoutError("timed out"), print_to_stdout=False)
        assert code == EXIT_TIMEOUT

    def test_asyncio_timeout_error_exit_code_5(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_TIMEOUT
        ser = ErrorSerializer()
        code = ser.serialize(asyncio.TimeoutError("async timeout"), print_to_stdout=False)
        assert code == EXIT_TIMEOUT

    def test_rate_limit_error_exit_code_7(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, _SDKRateLimitError, EXIT_RATE_LIMIT
        ser = ErrorSerializer()
        code = ser.serialize(_SDKRateLimitError("too many requests"), print_to_stdout=False)
        assert code == EXIT_RATE_LIMIT

    def test_import_error_exit_code_9(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_CONFIGURATION
        ser = ErrorSerializer()
        code = ser.serialize(ImportError("no module"), print_to_stdout=False)
        assert code == EXIT_CONFIGURATION

    def test_module_not_found_exit_code_9(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_CONFIGURATION
        ser = ErrorSerializer()
        code = ser.serialize(ModuleNotFoundError("missing module"), print_to_stdout=False)
        assert code == EXIT_CONFIGURATION

    def test_os_error_exit_code_9(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_CONFIGURATION
        ser = ErrorSerializer()
        code = ser.serialize(OSError("os failure"), print_to_stdout=False)
        assert code == EXIT_CONFIGURATION

    def test_unknown_exception_exit_code_1(self, capsys):
        """Unknown exception type → default to exit code 1 (UserInputError)."""
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_USER_INPUT
        ser = ErrorSerializer()
        code = ser.serialize(Exception("unknown"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT


class TestErrorSerializerHTTPClassification:
    """Test HTTP status code classification."""

    def test_http_401_returns_auth_code(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_AUTH
        ser = ErrorSerializer()
        exc = _mock_http_exception(401)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_AUTH

    def test_http_403_returns_permission_denied(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_PERMISSION_DENIED
        ser = ErrorSerializer()
        exc = _mock_http_exception(403)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_PERMISSION_DENIED

    def test_http_404_returns_not_found(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_NOT_FOUND
        ser = ErrorSerializer()
        exc = _mock_http_exception(404)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

    def test_http_409_returns_user_input(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_USER_INPUT
        ser = ErrorSerializer()
        exc = _mock_http_exception(409)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_http_429_returns_rate_limit(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_RATE_LIMIT
        ser = ErrorSerializer()
        exc = _mock_http_exception(429)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_RATE_LIMIT

    def test_http_500_returns_server_error(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_SERVER_ERROR
        ser = ErrorSerializer()
        exc = _mock_http_exception(500)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_SERVER_ERROR

    def test_http_502_returns_server_error(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_SERVER_ERROR
        ser = ErrorSerializer()
        exc = _mock_http_exception(502)
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_SERVER_ERROR

    def test_http_503_not_server_error(self, capsys):
        """503 should NOT be classified as ServerError (excluded per ADR-001)."""
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_SERVER_ERROR
        ser = ErrorSerializer()
        exc = _mock_http_exception(503)
        code = ser.serialize(exc, print_to_stdout=False)
        # 503 is excluded from ServerError classification
        assert code != EXIT_SERVER_ERROR

    def test_http_status_in_envelope(self, capsys):
        """HTTP status code included in error envelope."""
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer()
        exc = _mock_http_exception(404)
        ser.serialize(exc, print_to_stdout=True)
        captured = capsys.readouterr()
        envelope = json.loads(captured.out.strip())
        assert envelope["http_status"] == 404


class TestErrorSerializerErrorEnvelope:
    """Test JSON error envelope output."""

    def test_error_envelope_structure(self, capsys):
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer(call_id="test-id")
        ser.serialize(ValueError("test error"), print_to_stdout=True)
        captured = capsys.readouterr()
        envelope = json.loads(captured.out.strip())

        assert envelope["error"] is True
        assert "exit_code" in envelope
        assert "exit_code_name" in envelope
        assert envelope["message"] == "test error"
        assert envelope["exception_type"] == "ValueError"
        assert envelope["call_id"] == "test-id"
        assert "traceback" in envelope

    def test_error_envelope_no_stdout(self, capsys):
        """When print_to_stdout=False, nothing written to stdout."""
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer()
        ser.serialize(ValueError("test"), print_to_stdout=False)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_traceback_included(self, capsys):
        """Traceback string is included in envelope."""
        from foundry_cli.common.error_serializer import ErrorSerializer
        ser = ErrorSerializer()
        ser.serialize(RuntimeError("crash"), print_to_stdout=True)
        captured = capsys.readouterr()
        envelope = json.loads(captured.out.strip())
        # traceback.format_exception on a non-raised exception produces
        # the exception type and message (no "Traceback" header), so check
        # for the type name and message instead.
        assert "RuntimeError" in envelope["traceback"]
        assert "crash" in envelope["traceback"]


class TestErrorSerializerStaticMethods:
    """Test static utility methods."""

    def test_get_exit_code_name_known(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        assert ErrorSerializer.get_exit_code_name(0) == "Success"
        assert ErrorSerializer.get_exit_code_name(1) == "UserInputError"
        assert ErrorSerializer.get_exit_code_name(2) == "AuthenticationError"
        assert ErrorSerializer.get_exit_code_name(5) == "TimeoutError"
        assert ErrorSerializer.get_exit_code_name(9) == "ConfigurationError"

    def test_get_exit_code_name_unknown(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        assert ErrorSerializer.get_exit_code_name(99) == "UnknownError"

    def test_create_error_envelope(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        envelope = ErrorSerializer.create_error_envelope(
            exit_code=4,
            message="resource missing",
            exception_type="NotFoundError",
            call_id="abc"
        )
        assert envelope["error"] is True
        assert envelope["exit_code"] == 4
        assert envelope["exit_code_name"] == "NotFoundError"
        assert envelope["message"] == "resource missing"
        assert envelope["call_id"] == "abc"
        assert envelope["traceback"] == ""

    def test_create_error_envelope_auto_call_id(self):
        from foundry_cli.common.error_serializer import ErrorSerializer
        envelope = ErrorSerializer.create_error_envelope(1, "error")
        assert len(envelope["call_id"]) == 36  # UUID format


class TestErrorSerializerNestedExceptions:
    """Test MRO walking for nested/derived exception types."""

    def test_subclass_of_mapped_exception(self, capsys):
        """Custom subclass of mapped base → finds exit code via MRO."""
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_USER_INPUT
        ser = ErrorSerializer()
        class MyValueError(ValueError):
            pass
        code = ser.serialize(MyValueError("custom"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_deep_subclass(self, capsys):
        """Deeply nested subclass still resolves via MRO."""
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_NOT_FOUND
        ser = ErrorSerializer()
        class DeepNotFoundError(FileNotFoundError):
            pass
        class EvenDeeperError(DeepNotFoundError):
            pass
        code = ser.serialize(EvenDeeperError("deep"), print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

    def test_http_status_takes_precedence_over_type(self, capsys):
        """HTTP status classification takes precedence over exception type matching."""
        from foundry_cli.common.error_serializer import ErrorSerializer, EXIT_NOT_FOUND
        ser = ErrorSerializer()
        # ValueError normally → exit code 1, but HTTP 404 → exit code 4
        exc: Any = ValueError("not found")
        exc.response = MagicMock()
        exc.response.status_code = 404
        code = ser.serialize(exc, print_to_stdout=False)
        assert code == EXIT_NOT_FOUND


# ===========================================================================
# OutputFormatter — Unit Tests
# ===========================================================================

class TestOutputFormatterInit:
    """Test OutputFormatter initialization."""

    def test_default_auto_mode(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt.format_setting == "auto"

    def test_explicit_format_json(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        assert fmt.format_setting == "json"

    def test_explicit_format_toon(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        assert fmt.format_setting == "toon"

    def test_env_var_format(self, clean_output_env, monkeypatch):
        from foundry_cli.common.output_formatter import OutputFormatter
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT", "json")
        fmt = OutputFormatter()
        assert fmt.format_setting == "json"

    def test_pretty_default_false(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt.pretty is False

    def test_pretty_true(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(pretty=True)
        assert fmt.pretty is True


class TestOutputFormatterAutoSelection:
    """Test _select_format auto-selection algorithm (ADR-004)."""

    def test_step1_explicit_json_wins(self, clean_output_env):
        """Step 1: Explicit format always wins."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        assert fmt._select_format([{"a": 1}]) == "json"

    def test_step1_explicit_toon_wins(self, clean_output_env):
        """Step 1: Explicit TOON even for non-table data."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        assert fmt._select_format({"key": "val"}) == "toon"

    def test_step2_error_dict_uses_json(self, clean_output_env):
        """Step 2: Error dicts always use JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt._select_format({"error": True, "message": "fail"}) == "json"

    def test_step3_non_list_uses_json(self, clean_output_env):
        """Step 3: Non-list top-level (dict, scalar) → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt._select_format({"key": "val"}) == "json"
        assert fmt._select_format("string") == "json"
        assert fmt._select_format(42) == "json"
        assert fmt._select_format(None) == "json"

    def test_step4_empty_list_uses_json(self, clean_output_env):
        """Step 4: Empty list → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt._select_format([]) == "json"

    def test_step5_uniform_dicts_use_toon(self, clean_output_env):
        """Step 5-7: Uniform dict list → TOON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        data = [{"name": "a", "age": 1}, {"name": "b", "age": 2}]
        assert fmt._select_format(data) == "toon"

    def test_step6_non_dict_items_use_json(self, clean_output_env):
        """Step 6: List with non-dict items → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        assert fmt._select_format([{"a": 1}, "string"]) == "json"
        assert fmt._select_format([{"a": 1}, 42]) == "json"
        assert fmt._select_format([{"a": 1}, None]) == "json"

    def test_step7_mixed_field_sets_use_json(self, clean_output_env):
        """Step 7: Dicts with different keys → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        data = [{"name": "a"}, {"age": 2}]
        assert fmt._select_format(data) == "json"

    def test_step7_superset_field_sets_use_json(self, clean_output_env):
        """Dicts where one has extra fields → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        data = [{"name": "a", "age": 1}, {"name": "b"}]
        assert fmt._select_format(data) == "json"

    def test_single_uniform_dict_list(self, clean_output_env):
        """Single dict in list → TOON (uniform field set of size 1)."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        data = [{"name": "only"}]
        assert fmt._select_format(data) == "toon"


class TestOutputFormatterJSON:
    """Test JSON formatting."""

    def test_json_compact(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        result = fmt.format({"key": "value"})
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_json_pretty(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json", pretty=True)
        result = fmt.format({"key": "value"})
        # Pretty output should contain newlines and indentation
        assert "\n" in result
        assert "  " in result

    def test_json_list_data(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        data = [{"id": 1}, {"id": 2}]
        result = fmt.format(data)
        parsed = json.loads(result)
        assert parsed == [{"id": 1}, {"id": 2}]

    def test_json_scalar(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        assert fmt.format(42) == "42"
        assert fmt.format("hello") == '"hello"'

    def test_json_none(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        assert fmt.format(None) == "null"

    def test_json_with_non_serializable(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        # Using default=str to handle non-serializable
        result = fmt.format({"path": pathlib.Path(__file__)})
        parsed = json.loads(result)
        assert "path" in parsed


class TestOutputFormatterTOON:
    """Test TOON formatting."""

    def test_toon_uniform_data(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        data = [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
        result = fmt.format(data)
        assert "name" in result
        assert "Alice" in result
        assert "Bob" in result

    def test_toon_with_header_separator(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        data = [{"id": 1, "val": "a"}]
        result = fmt.format(data)
        lines = result.split("\n")
        assert len(lines) >= 3  # header + separator + data
        assert "-" in lines[1]  # separator line

    def test_toon_empty_list_returns_empty(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        result = fmt.format([])
        # Empty list should fall back to JSON per auto-selection
        assert result == "[]"

    def test_toon_column_alignment(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="toon")
        data = [
            {"name": "A", "long_field": "short"},
            {"name": "BBB", "long_field": "very long value"}
        ]
        result = fmt.format(data)
        assert "very long value" in result


class TestOutputFormatterFormatMethod:
    """Test format() method and edge cases."""

    def test_invalid_format_setting_raises(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="invalid")
        with pytest.raises(ValueError, match="Invalid format_setting"):
            fmt.format({"key": "val"})

    def test_format_fallback_to_json(self, clean_output_env):
        """When auto selects TOON but data isn't a list of dicts → JSON."""
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="auto")
        result = fmt.format({"key": "val"})
        assert json.loads(result) == {"key": "val"}

    def test_format_error_data(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="auto")
        error = {"error": True, "message": "failed"}
        result = fmt.format(error)
        parsed = json.loads(result)
        assert parsed == error

    def test_format_large_data(self, clean_output_env):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        large = [{"id": i, "name": f"item_{i}"} for i in range(1000)]
        result = fmt.format(large)
        parsed = json.loads(result)
        assert len(parsed) == 1000


class TestOutputFormatterEmit:
    """Test emit methods."""

    def test_emit_writes_to_stdout(self, clean_output_env, capsys):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter(format_setting="json")
        fmt.emit({"key": "value"})
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {"key": "value"}

    def test_emit_error_writes_to_stdout(self, clean_output_env, capsys):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        fmt.emit_error({"error": True, "msg": "fail"})
        captured = capsys.readouterr()
        envelope = json.loads(captured.out.strip())
        assert envelope["error"] is True

    def test_emit_to_stderr(self, clean_output_env, capsys):
        from foundry_cli.common.output_formatter import OutputFormatter
        fmt = OutputFormatter()
        fmt.emit_to_stderr({"meta": "data"})
        captured = capsys.readouterr()
        assert json.loads(captured.err.strip()) == {"meta": "data"}


# ===========================================================================
# LogSetup — Unit Tests
# ===========================================================================

class TestNdJsonFormatter:
    """Test _NdJsonFormatter directly."""

    def test_format_produces_valid_json(self):
        from foundry_cli.common.log_setup import _NdJsonFormatter
        formatter = _NdJsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Hello world",
            args=(),
            exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "ts" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["msg"] == "Hello world"

    def test_format_includes_context_fields(self):
        from foundry_cli.common.log_setup import _NdJsonFormatter
        formatter = _NdJsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="x.py",
            lineno=1, msg="retry", args=(), exc_info=None
        )
        record.op = "datasets.list"
        record.call_id = "abc-123"
        record.attempt = 2
        record.delay_ms = 1000
        record.http_status = 429
        result = formatter.format(record)
        parsed = json.loads(result)
        assert parsed["op"] == "datasets.list"
        assert parsed["call_id"] == "abc-123"
        assert parsed["attempt"] == 2
        assert parsed["delay_ms"] == 1000
        assert parsed["http_status"] == 429

    def test_format_skips_none_context(self):
        """Context fields with None value are omitted."""
        from foundry_cli.common.log_setup import _NdJsonFormatter
        formatter = _NdJsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="x.py",
            lineno=1, msg="test", args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "op" not in parsed
        assert "call_id" not in parsed

    def test_format_includes_exception_info(self):
        from foundry_cli.common.log_setup import _NdJsonFormatter
        formatter = _NdJsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys as _sys
            exc_info = _sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="x.py",
            lineno=1, msg="error occurred", args=(), exc_info=exc_info
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "exc" in parsed
        assert "ValueError" in parsed["exc"]

    def test_format_timestamp_is_iso8601(self):
        """Timestamp should be ISO 8601 format with timezone."""
        from foundry_cli.common.log_setup import _NdJsonFormatter
        formatter = _NdJsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="x.py",
            lineno=1, msg="test", args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        # Should contain 'T' for ISO 8601
        assert "T" in parsed["ts"]
        assert "+" in parsed["ts"] or "Z" in parsed["ts"]


class TestLogSetupConfigure:
    """Test LogSetup.configure() method."""

    def test_configure_returns_root_logger(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        logger = LogSetup.configure()
        assert logger is logging.getLogger()

    def test_configure_sets_default_level_warning(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure()
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_configure_explicit_level(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure(log_level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_configure_env_var_level(self, clean_log_env, monkeypatch):
        from foundry_cli.common.log_setup import LogSetup, ENV_LOG_LEVEL
        monkeypatch.setenv(ENV_LOG_LEVEL, "ERROR")
        LogSetup.configure()
        root = logging.getLogger()
        assert root.level == logging.ERROR

    def test_configure_explicit_overrides_env(self, clean_log_env, monkeypatch):
        from foundry_cli.common.log_setup import LogSetup, ENV_LOG_LEVEL
        monkeypatch.setenv(ENV_LOG_LEVEL, "ERROR")
        LogSetup.configure(log_level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_configure_invalid_level_raises(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        with pytest.raises(ValueError, match="Unsupported log level"):
            LogSetup.configure(log_level="INVALID")

    def test_configure_case_insensitive_level(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure(log_level="debug")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_configure_all_supported_levels(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup, SUPPORTED_LEVELS
        for level_name in SUPPORTED_LEVELS:
            LogSetup.reset()
            LogSetup.configure(log_level=level_name)
            root = logging.getLogger()
            assert root.level == getattr(logging, level_name), f"Failed for {level_name}"

    def test_configure_adds_stderr_handler(self, clean_log_env):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream == sys.stderr

    def test_configure_clears_existing_handlers(self, clean_log_env):
        """Existing handlers are cleared before adding new ones."""
        from foundry_cli.common.log_setup import LogSetup
        # Add a dummy handler; pytest may have added LogCaptureHandlers,
        # so we only care that our NullHandler is there before configure.
        root = logging.getLogger()
        null_handler = logging.NullHandler()
        root.addHandler(null_handler)
        assert null_handler in root.handlers

        LogSetup.configure()
        # After configure, only the stderr StreamHandler should remain.
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert root.handlers[0].stream == sys.stderr

    def test_configure_idempotent(self, clean_log_env):
        """Calling configure twice returns existing logger without adding handlers."""
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure()
        LogSetup.configure()
        root = logging.getLogger()
        assert len(root.handlers) == 1

    def test_configure_removes_null_handler(self, clean_log_env):
        """If NullHandler exists (Python 3.8+ behavior), it gets replaced."""
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure()
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "StreamHandler" in handler_types


class TestLogSetupReset:
    """Test LogSetup.reset() method."""

    def test_reset_clears_handlers(self):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure()
        LogSetup.reset()
        root = logging.getLogger()
        assert len(root.handlers) == 0

    def test_reset_allows_reconfigure(self):
        from foundry_cli.common.log_setup import LogSetup
        LogSetup.configure(log_level="DEBUG")
        LogSetup.reset()
        LogSetup.configure(log_level="ERROR")
        root = logging.getLogger()
        assert root.level == logging.ERROR
        assert len(root.handlers) == 1


class TestLogSetupMetadata:
    """Test metadata separator and emission."""

    def test_emit_metadata_separator(self, capsys):
        from foundry_cli.common.log_setup import LogSetup, METADATA_SEPARATOR
        LogSetup.emit_metadata_separator()
        captured = capsys.readouterr()
        assert captured.err.strip() == METADATA_SEPARATOR

    def test_emit_metadata_with_separator(self, capsys):
        from foundry_cli.common.log_setup import LogSetup, METADATA_SEPARATOR
        meta = {"page": 1, "total": 100}
        LogSetup.emit_metadata(meta)
        captured = capsys.readouterr()
        lines = captured.err.strip().split("\n")
        assert lines[0] == METADATA_SEPARATOR
        parsed = json.loads(lines[1])
        assert parsed == meta

    def test_metadata_separator_value(self):
        from foundry_cli.common.log_setup import METADATA_SEPARATOR
        assert METADATA_SEPARATOR == "# ---metadata-start---"


class TestGetLogger:
    """Test get_logger() convenience function."""

    def test_get_logger_returns_named_logger(self, clean_log_env):
        from foundry_cli.common.log_setup import get_logger
        logger = get_logger("test.module")
        assert logger.name == "test.module"

    def test_get_logger_configures_logging(self, clean_log_env):
        """get_logger should trigger LogSetup.configure()."""
        from foundry_cli.common.log_setup import get_logger, LogSetup
        LogSetup.reset()
        logger = get_logger("test")
        root = logging.getLogger()
        assert len(root.handlers) >= 1


class TestLogSetupIntegration:
    """Integration-level tests for logging behavior."""

    def test_log_record_written_to_stderr(self, clean_log_env, capsys):
        from foundry_cli.common.log_setup import LogSetup, get_logger
        LogSetup.configure(log_level="WARNING")
        logger = get_logger("test.integration")
        logger.warning("test warning", extra={"op": "test.op"})
        captured = capsys.readouterr()
        # Should be on stderr
        assert captured.err.strip()
        parsed = json.loads(captured.err.strip())
        assert parsed["msg"] == "test warning"
        assert parsed["op"] == "test.op"

    def test_debug_not_logged_at_warning_level(self, clean_log_env, capsys):
        from foundry_cli.common.log_setup import LogSetup, get_logger
        LogSetup.configure(log_level="WARNING")
        logger = get_logger("test.integration")
        logger.debug("should not appear")
        captured = capsys.readouterr()
        assert captured.err.strip() == ""

    def test_debug_logged_at_debug_level(self, clean_log_env, capsys):
        from foundry_cli.common.log_setup import LogSetup, get_logger
        LogSetup.configure(log_level="DEBUG")
        logger = get_logger("test.integration")
        logger.debug("should appear")
        captured = capsys.readouterr()
        parsed = json.loads(captured.err.strip())
        assert parsed["msg"] == "should appear"

    def test_info_not_logged_at_error_level(self, clean_log_env, capsys):
        from foundry_cli.common.log_setup import LogSetup, get_logger
        LogSetup.configure(log_level="ERROR")
        logger = get_logger("test.integration")
        logger.info("should not appear")
        captured = capsys.readouterr()
        assert captured.err.strip() == ""

    def test_critical_logged_at_any_level(self, clean_log_env, capsys):
        from foundry_cli.common.log_setup import LogSetup, get_logger
        LogSetup.configure(log_level="CRITICAL")
        logger = get_logger("test.integration")
        logger.critical("critical message")
        captured = capsys.readouterr()
        parsed = json.loads(captured.err.strip())
        assert parsed["level"] == "CRITICAL"


# ===========================================================================
# Exit Code Constants Verification
# ===========================================================================

class TestExitCodeConstants:
    """Verify ADR-001 exit code taxonomy constants."""

    def test_exit_code_values(self):
        from foundry_cli.common.error_serializer import (
            EXIT_SUCCESS, EXIT_USER_INPUT, EXIT_AUTH, EXIT_PERMISSION_DENIED,
            EXIT_NOT_FOUND, EXIT_TIMEOUT, EXIT_SERVER_ERROR, EXIT_RATE_LIMIT,
            EXIT_ACCESS_CONTROL, EXIT_CONFIGURATION
        )
        assert EXIT_SUCCESS == 0
        assert EXIT_USER_INPUT == 1
        assert EXIT_AUTH == 2
        assert EXIT_PERMISSION_DENIED == 3
        assert EXIT_NOT_FOUND == 4
        assert EXIT_TIMEOUT == 5
        assert EXIT_SERVER_ERROR == 6
        assert EXIT_RATE_LIMIT == 7
        assert EXIT_ACCESS_CONTROL == 8
        assert EXIT_CONFIGURATION == 9

    def test_exit_code_names_complete(self):
        from foundry_cli.common.error_serializer import EXIT_CODE_NAMES
        assert len(EXIT_CODE_NAMES) == 10  # codes 0-9
        assert all(i in EXIT_CODE_NAMES for i in range(10))


# ===========================================================================
# Retry Environment Variable Constants
# ===========================================================================

class TestRetryEnvConstants:
    """Verify retry environment variable constants."""

    def test_env_var_names(self):
        from foundry_cli.common.retry import (
            ENV_MAX_RETRIES, ENV_RETRY_BASE_DELAY,
            ENV_RETRY_MAX_DELAY, ENV_RETRY_JITTER
        )
        assert ENV_MAX_RETRIES == "FOUNDRY_MAX_RETRIES"
        assert ENV_RETRY_BASE_DELAY == "FOUNDRY_RETRY_BASE_DELAY"
        assert ENV_RETRY_MAX_DELAY == "FOUNDRY_RETRY_MAX_DELAY"
        assert ENV_RETRY_JITTER == "FOUNDRY_RETRY_JITTER"

    def test_default_values(self):
        from foundry_cli.common.retry import (
            DEFAULT_MAX_RETRIES, DEFAULT_BASE_DELAY,
            DEFAULT_MAX_DELAY, DEFAULT_JITTER
        )
        assert DEFAULT_MAX_RETRIES == 3
        assert DEFAULT_BASE_DELAY == 1.0
        assert DEFAULT_MAX_DELAY == 30.0
        assert DEFAULT_JITTER is True
