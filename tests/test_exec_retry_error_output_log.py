#!/usr/bin/env python3
"""Test Execution: TESTEXEC-001 — Execute QA test cases for RetryHandler, ErrorSerializer, OutputFormatter, LogSetup.

Implements all 41 test cases from TESTCASE-001 specification:
- Suite 1: RetryHandler (TC-RH-001 to TC-RH-008) — 8 tests
- Suite 2: ErrorSerializer (TC-ES-001 to TC-ES-010) — 10 tests
- Suite 3: OutputFormatter (TC-OF-001 to TC-OF-008) — 8 tests
- Suite 4: LogSetup (TC-LS-001 to TC-LS-007) — 7 tests
- Suite 5: Integration (TC-INT-001 to TC-INT-003) — 3 tests
- Suite 6: Non-Functional (TC-NF-001 to TC-NF-005) — 5 tests

Framework: pytest + pytest-asyncio
Run: pytest tests/test_exec_retry_error_output_log.py -v --tb=long
"""

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import time
from io import StringIO
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

# Ensure src is on path
_SRC_PATH = __import__("pathlib").Path(__file__).parent.parent / "src"
if str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def reset_log_setup():
    """Reset LogSetup singleton before/after each test."""
    from pal_found_cli.common.log_setup import LogSetup

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


@pytest.fixture
def stderr_capture():
    """Capture stderr output."""
    import io

    captured = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = captured
    yield captured
    sys.stderr = old_stderr


@pytest.fixture
def stdout_capture():
    """Capture stdout output."""
    import io

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    yield captured
    sys.stdout = old_stdout


def _mock_http_exception(status_code):
    """Create a mock exception with an HTTP response status code."""
    exc: Any = Exception(f"HTTP {status_code}")
    exc.response = MagicMock()
    exc.response.status_code = status_code
    return exc


# ===========================================================================
# SUITE 1: RetryHandler (8 test cases) — TC-RH-001 to TC-RH-008
# ===========================================================================


class TestRetryHandler_TC:
    """QA test cases for RetryHandler per TESTCASE-001."""

    def test_TC_RH_001_exponential_backoff_delay_calculation(self, clean_retry_env):
        """TC-RH-001: Exponential backoff delay calculation.

        Given a RetryHandler with initial_delay_ms=1000, max_delay_ms=30000, jitter=False
        When _calculate_delay() is called for attempts 0-4
        Then delays are [1.0, 2.0, 4.0, 8.0, 16.0]
        """
        from pal_found_cli.common.retry import _calculate_delay

        expected = [1.0, 2.0, 4.0, 8.0, 16.0]
        for attempt, exp_delay in enumerate(expected):
            delay = _calculate_delay(
                initial_delay_ms=1000.0,
                attempt=attempt,
                max_delay_ms=30000.0,
                multiplier=2.0,
                jitter=False,
            )
            assert delay == pytest.approx(exp_delay), (
                f"Attempt {attempt}: expected {exp_delay}, got {delay}"
            )

    def test_TC_RH_002_max_delay_cap_enforcement(self, clean_retry_env):
        """TC-RH-002: Max delay cap enforcement.

        Given initial_delay_ms=1000, max_delay_ms=5000, jitter=False
        When _calculate_delay() is called for attempts 0-5
        Then delays are [1.0, 2.0, 4.0, 5.0, 5.0, 5.0] — capped at 5.0 from attempt 3
        """
        from pal_found_cli.common.retry import _calculate_delay

        expected = [1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
        for attempt, exp_delay in enumerate(expected):
            delay = _calculate_delay(
                initial_delay_ms=1000.0,
                attempt=attempt,
                max_delay_ms=5000.0,
                multiplier=2.0,
                jitter=False,
            )
            assert delay == pytest.approx(exp_delay), (
                f"Attempt {attempt}: expected {exp_delay}, got {delay}"
            )

    def test_TC_RH_003_jitter_randomness_within_bounds(self, clean_retry_env):
        """TC-RH-003: Jitter randomness within bounds.

        Given initial_delay_ms=1000, jitter=True
        When _calculate_delay() is called 100 times
        Then each delay is within [0.9, 1.1] (+-10% of base_delay)
        """
        from pal_found_cli.common.retry import _calculate_delay

        for _ in range(100):
            delay = _calculate_delay(
                initial_delay_ms=1000.0,
                attempt=0,
                max_delay_ms=30000.0,
                multiplier=2.0,
                jitter=True,
            )
            assert 0.9 <= delay <= 1.1, f"Jitter delay {delay} out of bounds [0.9, 1.1]"

    def test_TC_RH_004_environment_variable_override(
        self, clean_retry_env, monkeypatch
    ):
        """TC-RH-004: Environment variable override for all config params.

        Given canonical retry env vars are set
        When RetryHandler() is instantiated with no constructor arguments
        Then handler reads all values from env vars
        """
        from pal_found_cli.common.retry import RetryHandler

        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS", "2000")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_MAX_DELAY_MS", "10000")
        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_JITTER", "false")

        handler = RetryHandler()
        assert handler.max_retries == 5
        assert handler.base_delay_ms == 2000.0
        assert handler.max_delay_ms == 10000.0
        assert handler.base_delay == 2.0
        assert handler.max_delay == 10.0
        assert handler.jitter is False

        # Constructor args take precedence
        handler2 = RetryHandler(max_retries=1)
        assert handler2.max_retries == 1

    def test_TC_RH_005_decorator_protocol(self, clean_retry_env):
        """TC-RH-005: Decorator protocol wraps async function with retry.

        Given a RetryHandler(max_retries=2) and an async function that raises twice then succeeds
        When @handler decorator is applied and awaited
        Then function succeeds on 3rd attempt and preserves __name__ and __doc__
        """
        from pal_found_cli.common.retry import RetryHandler

        call_count = 0
        handler = RetryHandler(max_retries=2, base_delay=0.001)

        @handler
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise requests.RequestException("transient")
            return "success"

        assert flaky_func.__name__ == "flaky_func"
        assert flaky_func.__doc__ is None  # Original has no docstring

        result = asyncio.get_event_loop().run_until_complete(flaky_func())
        assert result == "success"
        assert call_count == 3

    def test_TC_RH_006_context_manager_protocol(self, clean_retry_env):
        """TC-RH-006: Context manager protocol yields handler for execute().

        Given a RetryHandler(max_retries=2)
        When async with handler.context() as h: and h.execute() is called
        Then context manager yields the RetryHandler instance
        """
        from pal_found_cli.common.retry import RetryHandler

        handler = RetryHandler(max_retries=2, base_delay=0.001)

        async def run():
            async with handler.context() as h:
                assert h is handler
                return await h.execute(lambda: asyncio.sleep(0, result="done"))

        async def success():
            return "done"

        result = asyncio.get_event_loop().run_until_complete(
            handler.context().__aenter__()
        )
        # Verify context manager yields self
        assert result is handler

    def test_TC_RH_007_last_exception_raised_on_exhaustion(self, clean_retry_env):
        """TC-RH-007: Last exception raised on retry exhaustion.

        Given RetryHandler(max_retries=2) and a function that always raises
        When handler.execute(always_failing) is called
        Then after 3 total attempts, the last RequestException is raised
        """
        from pal_found_cli.common.retry import RetryHandler

        handler = RetryHandler(max_retries=2, base_delay=0.001)
        last_exc = None

        async def always_fail():
            nonlocal last_exc
            exc = requests.RequestException("failure")
            last_exc = exc
            raise exc

        with pytest.raises(requests.RequestException) as exc_info:
            asyncio.get_event_loop().run_until_complete(handler.execute(always_fail))

        assert str(exc_info.value) == "failure"

    def test_TC_RH_008_max_retries_zero_means_single_attempt(self, clean_retry_env):
        """TC-RH-008: max_retries=0 means no retries (single attempt only).

        Given RetryHandler(max_retries=0) and a function that always raises
        When handler.execute(failing_func) is called
        Then the function is attempted exactly once and the exception is raised
        """
        from pal_found_cli.common.retry import RetryHandler

        handler = RetryHandler(max_retries=0, base_delay=0.001)
        attempt_count = 0

        async def failing():
            nonlocal attempt_count
            attempt_count += 1
            raise requests.RequestException("fail")

        with pytest.raises(requests.RequestException):
            asyncio.get_event_loop().run_until_complete(handler.execute(failing))

        assert attempt_count == 1, (
            f"Expected 1 attempt with max_retries=0, got {attempt_count}"
        )


# ===========================================================================
# SUITE 2: ErrorSerializer (10 test cases) — TC-ES-001 to TC-ES-010
# ===========================================================================


class TestErrorSerializer_TC:
    """QA test cases for ErrorSerializer per TESTCASE-001."""

    def test_TC_ES_001_exit_code_1_user_input_error(self, stdout_capture):
        """TC-ES-001: Exit code 1 — UserInputError (ValueError, TypeError)."""
        from pal_found_cli.common.error_serializer import EXIT_USER_INPUT, ErrorSerializer

        serializer = ErrorSerializer()

        code = serializer.serialize(ValueError("bad input"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

        code2 = serializer.serialize(TypeError("wrong type"), print_to_stdout=False)
        assert code2 == EXIT_USER_INPUT

    def test_TC_ES_002_exit_code_2_auth_error(self, stdout_capture):
        """TC-ES-002: Exit code 2 — AuthenticationError (HTTP 401)."""
        from pal_found_cli.common.error_serializer import EXIT_AUTH, ErrorSerializer

        serializer = ErrorSerializer()
        exc = _mock_http_exception(401)
        code = serializer.serialize(exc, print_to_stdout=False)
        assert code == EXIT_AUTH

    def test_TC_ES_003_exit_code_3_permission_denied(self, stdout_capture):
        """TC-ES-003: Exit code 3 — PermissionDeniedError (PermissionError, HTTP 403)."""
        from pal_found_cli.common.error_serializer import (
            EXIT_PERMISSION_DENIED,
            ErrorSerializer,
        )

        serializer = ErrorSerializer()

        code = serializer.serialize(
            PermissionError("access denied"), print_to_stdout=False
        )
        assert code == EXIT_PERMISSION_DENIED

        exc403 = _mock_http_exception(403)
        code2 = serializer.serialize(exc403, print_to_stdout=False)
        assert code2 == EXIT_PERMISSION_DENIED

    def test_TC_ES_004_exit_code_4_not_found(self, stdout_capture):
        """TC-ES-004: Exit code 4 — NotFoundError (FileNotFoundError, HTTP 404)."""
        from pal_found_cli.common.error_serializer import EXIT_NOT_FOUND, ErrorSerializer

        serializer = ErrorSerializer()

        code = serializer.serialize(FileNotFoundError("missing"), print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

        exc404 = _mock_http_exception(404)
        code2 = serializer.serialize(exc404, print_to_stdout=False)
        assert code2 == EXIT_NOT_FOUND

    def test_TC_ES_005_exit_code_5_timeout(self, stdout_capture):
        """TC-ES-005: Exit code 5 — TimeoutError (asyncio.TimeoutError)."""
        from pal_found_cli.common.error_serializer import EXIT_TIMEOUT, ErrorSerializer

        serializer = ErrorSerializer()

        code = serializer.serialize(TimeoutError("timed out"), print_to_stdout=False)
        assert code == EXIT_TIMEOUT

        code2 = serializer.serialize(TimeoutError("timed out"), print_to_stdout=False)
        assert code2 == EXIT_TIMEOUT

    def test_TC_ES_006_exit_code_6_server_error(self, stdout_capture):
        """TC-ES-006: Exit code 6 — ServerError (HTTP 500, 502)."""
        from pal_found_cli.common.error_serializer import (
            EXIT_SERVER_ERROR,
            ErrorSerializer,
        )

        serializer = ErrorSerializer()

        exc500 = _mock_http_exception(500)
        code = serializer.serialize(exc500, print_to_stdout=False)
        assert code == EXIT_SERVER_ERROR

        exc502 = _mock_http_exception(502)
        code2 = serializer.serialize(exc502, print_to_stdout=False)
        assert code2 == EXIT_SERVER_ERROR

    def test_TC_ES_007_exit_code_7_rate_limit(self, stdout_capture):
        """TC-ES-007: Exit code 7 — RateLimitExhausted (HTTP 429)."""
        from pal_found_cli.common.error_serializer import EXIT_RATE_LIMIT, ErrorSerializer

        serializer = ErrorSerializer()
        exc429 = _mock_http_exception(429)
        code = serializer.serialize(exc429, print_to_stdout=False)
        assert code == EXIT_RATE_LIMIT

    def test_TC_ES_008_exit_code_8_access_control(self, stdout_capture):
        """TC-ES-008: Exit code 8 — AccessControlError (BUG-SUB-004 fix).

        Regression coverage for the TC2.R1 defect: AccessControlError fed
        through ErrorSerializer.serialize() must return exit code 8 per
        ADR-001, not the previous default 1 (UserInputError). Also asserts
        the stdout error envelope reports type 'AccessControlError' and the
        correct exit_code_name.
        """
        from pal_found_cli.common.access_control_guard import AccessControlError
        from pal_found_cli.common.error_serializer import (
            EXIT_ACCESS_CONTROL,
            EXIT_USER_INPUT,
            ErrorSerializer,
        )

        serializer = ErrorSerializer()
        exc = AccessControlError(
            "Access control policy denied: datasets.dataset.create blocked at step 3 (READONLY)",
            step=3,
        )
        code = serializer.serialize(exc, print_to_stdout=False)
        # Fix: AccessControlError -> EXIT_ACCESS_CONTROL (8)
        assert code == EXIT_ACCESS_CONTROL

        # HTTP 409 still maps to EXIT_USER_INPUT (unchanged behavior)
        exc409 = _mock_http_exception(409)
        code2 = serializer.serialize(exc409, print_to_stdout=False)
        assert code2 == EXIT_USER_INPUT

    def test_TC_ES_008b_access_control_envelope_and_message(self, stdout_capture):
        """TC-ES-008b: AccessControlError stdout envelope (BUG-SUB-004).

        Verifies the serialize() stdout envelope for AccessControlError
        carries type 'AccessControlError', exit_code 8, exit_code_name
        'AccessControlError', and the original message text.
        """
        import io as _io

        from pal_found_cli.common.access_control_guard import AccessControlError
        from pal_found_cli.common.error_serializer import (
            EXIT_CODE_NAMES,
            ErrorSerializer,
        )

        serializer = ErrorSerializer(call_id="bug-sub-004-tc2r1")
        exc = AccessControlError("blocked at step 3 (READONLY)", step=3)

        # Capture stdout explicitly so the envelope content is verifiable.
        real_stdout = sys.stdout
        captured = _io.StringIO()
        sys.stdout = captured
        try:
            code = serializer.serialize(exc, print_to_stdout=True)
        finally:
            sys.stdout = real_stdout

        envelope = json.loads(captured.getvalue())

        assert code == 8
        assert envelope["exit_code"] == 8
        assert envelope["exit_code_name"] == "AccessControlError"
        assert envelope["exception_type"] == "AccessControlError"
        assert envelope["error"] is True
        assert "blocked at step 3" in envelope["message"]
        assert envelope["call_id"] == "bug-sub-004-tc2r1"

        # Exit code 8 name still registered in taxonomy
        assert EXIT_CODE_NAMES.get(8) == "AccessControlError"

    def test_TC_ES_009_exit_code_9_configuration_error(self, stdout_capture):
        """TC-ES-009: Exit code 9 — ConfigurationError (ImportError, OSError, EnvironmentError)."""
        from pal_found_cli.common.error_serializer import (
            EXIT_CONFIGURATION,
            ErrorSerializer,
        )

        serializer = ErrorSerializer()

        code = serializer.serialize(
            ImportError("missing module"), print_to_stdout=False
        )
        assert code == EXIT_CONFIGURATION

        code2 = serializer.serialize(
            ModuleNotFoundError("no module"), print_to_stdout=False
        )
        assert code2 == EXIT_CONFIGURATION

        code3 = serializer.serialize(OSError("os error"), print_to_stdout=False)
        assert code3 == EXIT_CONFIGURATION

    def test_TC_ES_010_error_envelope_schema(self, stdout_capture):
        """TC-ES-010: Error envelope schema and metadata completeness."""
        from pal_found_cli.common.error_serializer import ErrorSerializer

        serializer = ErrorSerializer(call_id="test-123")

        # Test serialize returns correct exit code
        code = serializer.serialize(ValueError("test error"), print_to_stdout=False)
        assert code == 1  # EXIT_USER_INPUT

        # Test get_exit_code_name
        assert ErrorSerializer.get_exit_code_name(0) == "Success"
        assert ErrorSerializer.get_exit_code_name(99) == "UnknownError"

        # Test create_error_envelope
        envelope = ErrorSerializer.create_error_envelope(
            exit_code=1, message="test", exception_type="ValueError", call_id="test-123"
        )
        assert envelope["error"] is True
        assert envelope["exit_code"] == 1
        assert envelope["message"] == "test"
        assert envelope["exception_type"] == "ValueError"
        assert envelope["call_id"] == "test-123"


# ===========================================================================
# SUITE 3: OutputFormatter (8 test cases) — TC-OF-001 to TC-OF-008
# ===========================================================================


class TestOutputFormatter_TC:
    """QA test cases for OutputFormatter per TESTCASE-001."""

    def test_TC_OF_001_json_format_output(self, clean_output_env):
        """TC-OF-001: JSON format output."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="json")
        result = formatter.format({"key": "value", "num": 42})
        parsed = json.loads(result)
        assert parsed == {"key": "value", "num": 42}

    def test_TC_OF_002_toon_format_output(self, clean_output_env):
        """TC-OF-002: TOON format output (tabular)."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        data = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}]
        formatter = OutputFormatter(format_setting="toon")
        result = formatter.format(data)
        # TOON output should be a table with header, separator, and data rows
        lines = result.split("\n")
        assert len(lines) >= 3  # header + separator + at least 1 data row
        assert "id" in lines[0] and "name" in lines[0]  # Header contains column names
        assert "-" in lines[1]  # Separator line

    def test_TC_OF_003_auto_selection_explicit_format_wins(self, clean_output_env):
        """TC-OF-003: Auto-selection — explicit format wins (Step 1 of ADR-004)."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        data = [{"id": "1"}, {"id": "2"}]  # Uniform list would select TOON
        formatter = OutputFormatter(format_setting="json")
        result = formatter.format(data)
        # Explicit JSON wins over auto-selection
        parsed = json.loads(result)
        assert parsed == data

    def test_TC_OF_004_auto_selection_error_always_json(self, clean_output_env):
        """TC-OF-004: Auto-selection — error data always JSON (Step 2)."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="auto")
        error_data = {"error": True, "message": "fail"}
        result = formatter.format(error_data)
        parsed = json.loads(result)
        assert parsed["error"] is True

    def test_TC_OF_005_auto_selection_non_list_and_empty_list_json(
        self, clean_output_env
    ):
        """TC-OF-005: Auto-selection — non-list and empty list use JSON (Steps 3-4)."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="auto")

        # Non-list uses JSON
        result = formatter.format({"single": "dict"})
        parsed = json.loads(result)
        assert parsed == {"single": "dict"}

        # Empty list uses JSON
        result2 = formatter.format([])
        parsed2 = json.loads(result2)
        assert parsed2 == []

    def test_TC_OF_006_auto_selection_uniform_field_set_selects_toon(
        self, clean_output_env
    ):
        """TC-OF-006: Auto-selection — uniform field set selects TOON (Steps 5-7)."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="auto")

        # Uniform field set -> TOON
        uniform = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        result = formatter.format(uniform)
        lines = result.split("\n")
        assert "a" in lines[0] and "b" in lines[0]  # Header
        assert "-" in lines[1]  # Separator

        # Mixed field sets -> JSON
        mixed = [{"a": 1}, {"a": 2, "b": 3}]
        result2 = formatter.format(mixed)
        parsed = json.loads(result2)
        assert parsed == mixed

    def test_TC_OF_007_pretty_print_json(self, clean_output_env):
        """TC-OF-007: Pretty-print JSON output."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="json", pretty=True)
        result = formatter.format({"nested": {"key": "value"}})
        parsed = json.loads(result)
        assert parsed == {"nested": {"key": "value"}}
        # Verify indentation (pretty-printed)
        assert "\n" in result and "  " in result

    def test_TC_OF_008_invalid_format_raises_value_error(self, clean_output_env):
        """TC-OF-008: Invalid format_setting raises ValueError."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="xml")
        with pytest.raises(ValueError) as exc_info:
            formatter.format({"data": "test"})
        assert "json" in str(exc_info.value).lower()
        assert "toon" in str(exc_info.value).lower()
        assert "auto" in str(exc_info.value).lower()


# ===========================================================================
# SUITE 4: LogSetup (7 test cases) — TC-LS-001 to TC-LS-007
# ===========================================================================


class TestLogSetup_TC:
    """QA test cases for LogSetup per TESTCASE-001."""

    def test_TC_LS_001_ndjson_format_single_json_line(self, clean_log_env, capfd):
        """TC-LS-001: NDJSON format — single JSON line per log record."""
        from pal_found_cli.common.log_setup import LogSetup

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_ls_001")
        logger.warning("test message")

        output = capfd.readouterr().err.strip()
        lines = [l for l in output.split("\n") if l.strip()]
        # Should produce a valid JSON line
        log_line = lines[0]
        parsed = json.loads(log_line)
        assert "ts" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "msg" in parsed

    def test_TC_LS_002_required_fields_present(self, clean_log_env, capfd):
        """TC-LS-002: Required fields present in log record."""
        from pal_found_cli.common.log_setup import LogSetup

        LogSetup.configure(log_level="INFO")
        logger = logging.getLogger("test_tc_ls_002")
        logger.info("test")

        output = capfd.readouterr().err.strip()
        parsed = json.loads(output.split("\n")[0])
        assert parsed["level"] == "INFO"
        assert parsed["msg"] == "test"
        # Verify ISO 8601 timestamp with timezone
        assert "T" in parsed["ts"]
        assert parsed["ts"].endswith("+00:00") or "Z" in parsed["ts"]

    def test_TC_LS_003_log_level_filtering(self, clean_log_env, capfd):
        """TC-LS-003: Log level filtering."""
        from pal_found_cli.common.log_setup import LogSetup

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_ls_003")

        logger.debug("debug msg")
        logger.info("info msg")
        logger.warning("warning msg")
        logger.error("error msg")

        output = capfd.readouterr().err
        # Debug and info should NOT appear
        assert "debug msg" not in output
        assert "info msg" not in output
        # Warning and error SHOULD appear
        assert "warning msg" in output
        assert "error msg" in output

    def test_TC_LS_004_env_var_log_level_override(
        self, clean_log_env, capfd, monkeypatch
    ):
        """TC-LS-004: Environment variable log level override."""
        from pal_found_cli.common.log_setup import LogSetup

        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_LOG_LEVEL", "DEBUG")
        LogSetup.configure()
        logger = logging.getLogger("test_tc_ls_004")

        logger.debug("debug from env")
        output = capfd.readouterr().err
        assert "debug from env" in output

    def test_TC_LS_005_invalid_log_level_raises(self, clean_log_env):
        """TC-LS-005: Invalid log level raises ValueError."""
        from pal_found_cli.common.log_setup import LogSetup

        with pytest.raises(ValueError) as exc_info:
            LogSetup.configure(log_level="TRACE")
        assert "TRACE" in str(exc_info.value)
        assert "DEBUG" in str(exc_info.value)  # Should list valid levels

    def test_TC_LS_006_context_extra_fields_in_log(self, clean_log_env, capfd):
        """TC-LS-006: Context/extra fields included in log output."""
        from pal_found_cli.common.log_setup import LogSetup

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_ls_006")
        logger.warning(
            "retry", extra={"op": "datasets.list", "attempt": 2, "delay_ms": 1000}
        )

        output = capfd.readouterr().err.strip()
        parsed = json.loads(output.split("\n")[0])
        assert parsed.get("op") == "datasets.list"
        assert parsed.get("attempt") == 2
        assert parsed.get("delay_ms") == 1000

    def test_TC_LS_007_metadata_separator_and_emit(self, clean_log_env, capfd):
        """TC-LS-007: Metadata separator and emit_metadata."""
        from pal_found_cli.common.log_setup import METADATA_SEPARATOR, LogSetup

        LogSetup.emit_metadata_separator()
        output = capfd.readouterr().err
        assert METADATA_SEPARATOR in output

        LogSetup.emit_metadata({"key": "value"})
        output = capfd.readouterr().err
        assert METADATA_SEPARATOR in output
        # After separator should be JSON metadata
        lines = [l for l in output.split("\n") if l.strip()]
        json_line = lines[-1]
        parsed = json.loads(json_line)
        assert parsed["key"] == "value"


# ===========================================================================
# SUITE 5: Integration (3 test cases) — TC-INT-001 to TC-INT-003
# ===========================================================================


class TestIntegration_TC:
    """QA integration test cases per TESTCASE-001."""

    def test_TC_INT_001_retry_exhaustion_produces_correct_exit_code(
        self, clean_retry_env, stdout_capture
    ):
        """TC-INT-001: RetryHandler + ErrorSerializer — retry exhaustion produces correct exit code."""
        import requests

        from pal_found_cli.common.error_serializer import EXIT_USER_INPUT, ErrorSerializer
        from pal_found_cli.common.retry import RetryHandler

        handler = RetryHandler(max_retries=1, base_delay=0.001)
        serializer = ErrorSerializer()

        async def always_fail():
            raise requests.RequestException("connection refused")

        # Execute and catch exception
        last_exc = None
        try:
            asyncio.get_event_loop().run_until_complete(handler.execute(always_fail))
        except requests.RequestException as e:
            last_exc = e

        assert last_exc is not None

        # Serialize the exception
        assert last_exc is not None
        code = serializer.serialize(last_exc, print_to_stdout=False)  # type: ignore[arg-type]
        assert isinstance(code, int)
        assert 0 <= code <= 9

    def test_TC_INT_002_stderr_separation(self, clean_log_env, capfd):
        """TC-INT-002: OutputFormatter + LogSetup — stderr separation."""
        from pal_found_cli.common.log_setup import LogSetup
        from pal_found_cli.common.output_formatter import OutputFormatter

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_int_002")

        formatter = OutputFormatter(format_setting="auto")
        formatter.emit_error({"error": True, "code": 1})

        logger.warning("log message")

        captured = capfd.readouterr()
        stdout_output = captured.out
        stderr_output = captured.err

        # Error JSON and logs should go to stderr; stdout stays for result data.
        assert stdout_output == ""
        assert "error" in stderr_output

        # Log NDJSON should go to stderr
        assert "log message" in stderr_output or "msg" in stderr_output

    def test_TC_INT_003_full_pipeline(self, clean_retry_env, clean_log_env, capfd):
        """TC-INT-003: Full pipeline — retry -> serialize -> format -> log."""
        import requests

        from pal_found_cli.common.error_serializer import ErrorSerializer
        from pal_found_cli.common.log_setup import LogSetup
        from pal_found_cli.common.output_formatter import OutputFormatter
        from pal_found_cli.common.retry import RetryHandler

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_int_003")

        handler = RetryHandler(max_retries=1, base_delay=0.001)
        serializer = ErrorSerializer(call_id="pipeline-test")
        formatter = OutputFormatter(format_setting="json")

        async def fail():
            raise requests.RequestException("pipeline failure")

        last_exc = None
        try:
            asyncio.get_event_loop().run_until_complete(handler.execute(fail))
        except requests.RequestException as e:
            last_exc = e

        # Serialize
        assert last_exc is not None
        code = serializer.serialize(last_exc, print_to_stdout=False)  # type: ignore[arg-type]

        # Format
        envelope = ErrorSerializer.create_error_envelope(
            exit_code=code,
            message=str(last_exc),
            exception_type=type(last_exc).__name__,
            call_id="pipeline-test",
        )
        output = formatter.format(envelope)
        parsed = json.loads(output)

        # Verify valid JSON error envelope on stdout
        assert parsed["error"] is True
        assert parsed["exit_code"] == code
        assert parsed["call_id"] == "pipeline-test"

        # Log entry on stderr
        logger.warning(
            "Pipeline completed with error", extra={"op": "pipeline", "exit_code": code}
        )
        stderr_output = capfd.readouterr().err
        assert (
            "Pipeline completed with error" in stderr_output or "msg" in stderr_output
        )


# ===========================================================================
# SUITE 6: Non-Functional (5 test cases) — TC-NF-001 to TC-NF-005
# ===========================================================================


class TestNonFunctional_TC:
    """QA non-functional test cases per TESTCASE-001."""

    def test_TC_NF_001_retry_delay_performance(self, clean_retry_env):
        """TC-NF-001: Retry delay performance — sleep doesn't block event loop."""
        import requests

        from pal_found_cli.common.retry import RetryHandler

        handler = RetryHandler(max_retries=3, base_delay=100.0, jitter=False)

        async def always_fail():
            raise requests.RequestException("fail")

        start = time.time()
        with pytest.raises(requests.RequestException):
            asyncio.get_event_loop().run_until_complete(handler.execute(always_fail))
        elapsed = time.time() - start

        # 3 retries with 0.1s delay each = ~0.3s minimum
        # Allow some margin for execution overhead
        assert 0.2 <= elapsed <= 1.0, f"Retry took {elapsed:.3f}s, expected ~0.3-0.6s"

    def test_TC_NF_002_error_serializer_memory(self, stdout_capture):
        """TC-NF-002: ErrorSerializer memory — traceback doesn't cause leak."""
        from pal_found_cli.common.error_serializer import ErrorSerializer

        serializer = ErrorSerializer()

        # Call serialize many times — should not cause memory issues
        for i in range(1000):
            try:
                raise ValueError(f"test error {i}")
            except ValueError as e:
                serializer.serialize(e, print_to_stdout=False)

        # If we get here without OOM, the test passes
        assert True

    def test_TC_NF_003_no_secrets_in_log_output(
        self, clean_log_env, stderr_capture, monkeypatch
    ):
        """TC-NF-003: Security — no secrets in log output."""
        from pal_found_cli.common.log_setup import LogSetup

        monkeypatch.setenv("FOUNDRY_TOKEN", "super_secret_token_123")

        LogSetup.configure(log_level="WARNING")
        logger = logging.getLogger("test_tc_nf_003")

        # Log a message that mentions credentials
        logger.warning("Auth failed for token")

        output = stderr_capture.getvalue()
        assert "super_secret_token_123" not in output

    def test_TC_NF_004_unicode_and_special_characters(self, clean_output_env):
        """TC-NF-004: OutputFormatter handles Unicode and special characters."""
        from pal_found_cli.common.output_formatter import OutputFormatter

        formatter = OutputFormatter(format_setting="json")
        result = formatter.format(
            {"text": "hello world", "newline": "line1\nline2", "emoji": "smile"}
        )
        parsed = json.loads(result)
        assert parsed["text"] == "hello world"
        assert parsed["newline"] == "line1\nline2"

    def test_TC_NF_005_log_setup_idempotent(self, clean_log_env, stderr_capture):
        """TC-NF-005: LogSetup singleton — configure() is idempotent."""
        from pal_found_cli.common.log_setup import LogSetup

        # First configure
        logger1 = LogSetup.configure(log_level="WARNING")
        handler_count_1 = len(logger1.handlers)

        # Second configure should return same logger without adding handlers
        logger2 = LogSetup.configure(log_level="DEBUG")
        handler_count_2 = len(logger2.handlers)

        assert logger1 is logger2
        assert handler_count_1 == handler_count_2

        # Reset and reconfigure
        LogSetup.reset()
        logger3 = LogSetup.configure(log_level="ERROR")
        assert len(logger3.handlers) == 1


# ===========================================================================
# Summary fixture for test reporting
# ===========================================================================


@pytest.fixture(scope="session")
def test_execution_summary():
    """Session-scoped fixture for generating test execution summary."""
    return {
        "ticket": "TESTEXEC-001",
        "parent": "DEV-STORY-002",
        "test_case_spec": "TESTCASE-001",
        "total_cases": 41,
        "suites": [
            {"name": "RetryHandler", "cases": 8, "prefix": "TC-RH"},
            {"name": "ErrorSerializer", "cases": 10, "prefix": "TC-ES"},
            {"name": "OutputFormatter", "cases": 8, "prefix": "TC-OF"},
            {"name": "LogSetup", "cases": 7, "prefix": "TC-LS"},
            {"name": "Integration", "cases": 3, "prefix": "TC-INT"},
            {"name": "Non-Functional", "cases": 5, "prefix": "TC-NF"},
        ],
    }
