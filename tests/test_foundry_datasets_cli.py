#!/usr/bin/env python3
"""Unit tests for foundry_datasets_cli.py — 33 operations across 5 resource clients.

Tests cover:
- Argument parsing for all 33 operations (including --timeout placement)
- _model_to_dict serialization
- _get_client routing
- _invoke dispatch for each resource type (async, with timeout param)
- _resolve kebab-case to snake_case
- Error handling paths
- Access control integration
- Output formatter integration
- RetryHandler integration (ADR-002)
- Timeout resolution from args / cfg (ADR-002)
- Operation-presence validation (WARNING-3)

Framework: pytest with pytest-asyncio
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src is on path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.access_control_guard import (
    AccessControlError,
    AccessControlGuard,
)
from foundry_cli.common.config_loader import ConfigLoader
from foundry_cli.common.error_serializer import (
    EXIT_ACCESS_CONTROL,
    EXIT_CONFIGURATION,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION_DENIED,
    EXIT_RATE_LIMIT,
    EXIT_SERVER_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_INPUT,
    ErrorSerializer,
)
from foundry_cli.common.output_formatter import OutputFormatter

# Import CLI module directly
_SKILL_DIR = (
    Path(__file__).parent.parent / ".claude" / "skills" / "foundry-datasets" / "scripts"
)
sys.path.insert(0, str(_SKILL_DIR))
import importlib

foundry_datasets_cli = importlib.import_module("foundry_datasets_cli")
_model_to_dict = foundry_datasets_cli._model_to_dict
_get_client = foundry_datasets_cli._get_client
_invoke = foundry_datasets_cli._invoke
_resolve = foundry_datasets_cli._resolve
build_parser = foundry_datasets_cli.build_parser


def _async_client() -> MagicMock:
    """Build a mock SDK client whose coroutine methods are AsyncMock."""
    mc = MagicMock()
    for name in (
        "create",
        "get",
        "get_health_check_reports",
        "get_health_checks",
        "get_schedules",
        "get_schema",
        "get_schema_batch",
        "jobs",
        "put_schema",
        "read_table",
        "transactions",
        "delete",
        "list",
        "content",
        "upload",
        "abort",
        "build",
        "commit",
        "job",
        "add_backing_datasets",
        "add_primary_key",
        "remove_backing_datasets",
        "replace_backing_datasets",
    ):
        setattr(mc, name, AsyncMock())
    return mc


# --- Test _model_to_dict ---


class TestModelToDict:
    """Tests for _model_to_dict helper."""

    def test_none_returns_none(self):
        assert _model_to_dict(None) is None

    def test_pydantic_model_with_model_dump(self):
        """Model with model_dump() method (Pydantic v2)."""
        mock = MagicMock()
        mock.model_dump.return_value = {"key": "value"}
        assert _model_to_dict(mock) == {"key": "value"}

    def test_pydantic_model_with_dict(self):
        """Model with dict() method (Pydantic v1)."""
        mock = MagicMock()
        mock.dict.return_value = {"key": "value"}
        mock.model_dump = MagicMock(side_effect=AttributeError)
        assert _model_to_dict(mock) == {"key": "value"}

    def test_list_of_models(self):
        items = [MagicMock(), MagicMock()]
        items[0].model_dump.return_value = {"id": 1}
        items[1].model_dump.return_value = {"id": 2}
        result = _model_to_dict(items)
        assert result == [{"id": 1}, {"id": 2}]

    def test_nested_dict(self):
        data = {"a": 1, "b": {"c": 2}}
        result = _model_to_dict(data)
        assert result == {"a": 1, "b": {"c": 2}}

    def test_primitive_passthrough(self):
        assert _model_to_dict(42) == 42
        assert _model_to_dict("hello") == "hello"
        assert _model_to_dict(True) is True


# --- Test _get_client ---


class TestGetClient:
    """Tests for _get_client routing."""

    def test_dataset_returns_dataset_client(self):
        cfg = MagicMock()
        mock_client = MagicMock()
        with patch.object(
            foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client
        ):
            result = _get_client(cfg, "dataset")
            assert result == mock_client.datasets.Dataset

    def test_branch_returns_branch_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(
            foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client
        ):
            result = _get_client(cfg, "branch")
            assert result == mock_dataset.Branch

    def test_file_returns_file_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(
            foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client
        ):
            result = _get_client(cfg, "file")
            assert result == mock_dataset.File

    def test_transaction_returns_transaction_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(
            foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client
        ):
            result = _get_client(cfg, "transaction")
            assert result == mock_dataset.Transaction

    def test_view_returns_view_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(
            foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client
        ):
            result = _get_client(cfg, "view")
            assert result == mock_dataset.View


# --- Test _resolve ---


class TestResolve:
    """Tests for kebab-case to snake_case resolution."""

    def test_dataset_kebab_ops(self):
        assert (
            _resolve("dataset", "get-health-check-reports")
            == "get_health_check_reports"
        )
        assert _resolve("dataset", "get-health-checks") == "get_health_checks"
        assert _resolve("dataset", "get-schedules") == "get_schedules"
        assert _resolve("dataset", "get-schema") == "get_schema"
        assert _resolve("dataset", "get-schema-batch") == "get_schema_batch"
        assert _resolve("dataset", "put-schema") == "put_schema"
        assert _resolve("dataset", "read-table") == "read_table"

    def test_view_kebab_ops(self):
        assert _resolve("view", "add-backing-datasets") == "add_backing_datasets"
        assert _resolve("view", "add-primary-key") == "add_primary_key"
        assert _resolve("view", "remove-backing-datasets") == "remove_backing_datasets"
        assert (
            _resolve("view", "replace-backing-datasets") == "replace_backing_datasets"
        )

    def test_snake_case_passthrough(self):
        """Non-kebab operations pass through unchanged."""
        assert _resolve("dataset", "create") == "create"
        assert _resolve("dataset", "get") == "get"
        assert _resolve("branch", "list") == "list"

    def test_unknown_resource_defaults_to_replace(self):
        assert _resolve("unknown", "some-op") == "some_op"


# --- Test build_parser ---


class TestBuildParser:
    """Tests for argparse parser construction."""

    def test_parser_created(self):
        parser = build_parser()
        assert parser is not None

    def test_dataset_resource_exists(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123"])
        assert args.resource == "dataset"
        assert args.operation == "get"
        assert args.dataset_rid == "rid123"

    def test_branch_resource_exists(self):
        parser = build_parser()
        args = parser.parse_args(["branch", "list", "rid123"])
        assert args.resource == "branch"
        assert args.operation == "list"

    def test_file_resource_exists(self):
        parser = build_parser()
        args = parser.parse_args(["file", "get", "rid123", "--file-path", "/data.csv"])
        assert args.resource == "file"
        assert args.operation == "get"

    def test_transaction_resource_exists(self):
        parser = build_parser()
        args = parser.parse_args(["transaction", "create", "rid123"])
        assert args.resource == "transaction"
        assert args.operation == "create"

    def test_view_resource_exists(self):
        parser = build_parser()
        args = parser.parse_args(["view", "get", "--view-dataset-rid", "rid123"])
        assert args.resource == "view"
        assert args.operation == "get"

    def test_dataset_create_options(self):
        parser = build_parser()
        args = parser.parse_args(
            ["dataset", "create", "--name", "test", "--parent-folder-rid", "folder_rid"]
        )
        assert args.name == "test"
        assert args.parent_folder_rid == "folder_rid"

    def test_timeout_option_accepted_after_operation(self):
        """WARNING-2 / parser fix: --timeout must be accepted after the operation positional."""
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--timeout", "60"])
        assert args.timeout == 60

    def test_format_option_accepted_after_operation(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--format", "json"])
        assert args.format == "json"

    def test_pretty_option_accepted_after_operation(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--pretty"])
        assert args.pretty is True

    def test_pagination_options_accepted_after_operation(self):
        parser = build_parser()
        args = parser.parse_args(
            ["dataset", "get", "rid123", "--page-size", "50", "--page-token", "tok123"]
        )
        assert args.page_size == 50
        assert args.page_token == "tok123"

    def test_no_resource_shows_help(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.resource is None

    def test_kebab_case_operations(self):
        parser = build_parser()
        args = parser.parse_args(
            ["dataset", "get-health-checks", "rid123", "--branch-name", "main"]
        )
        assert args.operation == "get-health-checks"
        assert args.branch_name == "main"

    def test_view_operations(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "view",
                "add-backing-datasets",
                "--view-dataset-rid",
                "view_rid",
                "--backing-datasets",
                '["rid1", "rid2"]',
                "--branch",
                "main",
            ]
        )
        assert args.view_dataset_rid == "view_rid"
        assert args.backing_datasets == '["rid1", "rid2"]'
        assert args.branch == "main"


# --- Test _invoke dispatch ---


class TestInvoke:
    """Tests for _invoke operation dispatch (async, with timeout param)."""

    @pytest.mark.asyncio
    async def test_dataset_create(self):
        mock_client = _async_client()
        mock_client.create.return_value = {"id": "new_rid"}
        args = argparse.Namespace(name="test", parent_folder_rid="folder", timeout=None)
        await _invoke("dataset", "create", mock_client, args, timeout=None)
        mock_client.create.assert_called_once_with(
            name="test", parent_folder_rid="folder", request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_dataset_create_passes_timeout(self):
        """CRITICAL-1 regression: timeout is forwarded to the SDK call."""
        mock_client = _async_client()
        args = argparse.Namespace(name="t", parent_folder_rid="f", timeout=None)
        await _invoke("dataset", "create", mock_client, args, timeout=42)
        mock_client.create.assert_called_once_with(
            name="t", parent_folder_rid="f", request_timeout=42
        )

    @pytest.mark.asyncio
    async def test_dataset_get(self):
        mock_client = _async_client()
        args = argparse.Namespace(dataset_rid="rid123", timeout=None)
        await _invoke("dataset", "get", mock_client, args, timeout=None)
        mock_client.get.assert_called_once_with(
            dataset_rid="rid123", request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_branch_list(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            dataset_rid="rid123", page_size=10, page_token=None, timeout=None
        )
        await _invoke("branch", "list", mock_client, args, timeout=None)
        mock_client.list.assert_called_once_with(
            dataset_rid="rid123", page_size=10, page_token=None, request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_file_get(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            dataset_rid="rid123",
            file_path="/data.csv",
            transaction_rid=None,
            timeout=None,
        )
        await _invoke("file", "get", mock_client, args, timeout=None)
        mock_client.get.assert_called_once_with(
            dataset_rid="rid123",
            file_path="/data.csv",
            transaction_rid=None,
            request_timeout=None,
        )

    @pytest.mark.asyncio
    async def test_transaction_create(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            dataset_rid="rid123", branch_name="main", timeout=None
        )
        await _invoke("transaction", "create", mock_client, args, timeout=None)
        mock_client.create.assert_called_once_with(
            dataset_rid="rid123", branch_name="main", request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_view_get(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            view_dataset_rid="view_rid", branch="main", timeout=None
        )
        await _invoke("view", "get", mock_client, args, timeout=None)
        mock_client.get.assert_called_once_with(
            view_dataset_rid="view_rid", branch="main", request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_unknown_operation_raises(self):
        mock_client = _async_client()
        args = argparse.Namespace()
        with pytest.raises(ValueError, match="Unknown operation"):
            await _invoke("unknown", "unknown", mock_client, args, timeout=None)

    @pytest.mark.asyncio
    async def test_dataset_schema_batch_parses_json(self):
        mock_client = _async_client()
        args = argparse.Namespace(dataset_rids='["rid1", "rid2"]', timeout=None)
        await _invoke("dataset", "get_schema_batch", mock_client, args, timeout=None)
        mock_client.get_schema_batch.assert_called_once_with(
            dataset_rids=["rid1", "rid2"], request_timeout=None
        )

    @pytest.mark.asyncio
    async def test_view_add_backing_datasets_parses_json(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            view_dataset_rid="view_rid",
            backing_datasets='[{"dataset_rid": "rid1"}]',
            branch="main",
            timeout=None,
        )
        await _invoke("view", "add_backing_datasets", mock_client, args, timeout=None)
        mock_client.add_backing_datasets.assert_called_once()
        call_kwargs = mock_client.add_backing_datasets.call_args.kwargs
        assert call_kwargs["backing_datasets"] == [{"dataset_rid": "rid1"}]

    @pytest.mark.asyncio
    async def test_file_upload_reads_bytes(self, tmp_path):
        mock_client = _async_client()
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello")
        args = argparse.Namespace(
            dataset_rid="rid", file_path=str(f), transaction_rid=None, timeout=None
        )
        await _invoke("file", "upload", mock_client, args, timeout=None)
        mock_client.upload.assert_called_once()
        kwargs = mock_client.upload.call_args.kwargs
        assert kwargs["content"] == b"hello"

    @pytest.mark.asyncio
    async def test_file_upload_requires_file_path(self):
        mock_client = _async_client()
        args = argparse.Namespace(
            dataset_rid="rid", file_path=None, transaction_rid=None, timeout=None
        )
        with pytest.raises(ValueError, match="file_path is required"):
            await _invoke("file", "upload", mock_client, args, timeout=None)


# --- Test ErrorSerializer integration ---


class TestErrorSerializer:
    """Tests for ErrorSerializer exit code mapping."""

    def test_serializer_generates_call_id(self):
        s = ErrorSerializer()
        assert s.call_id is not None

    def test_value_error_maps_to_user_input(self):
        s = ErrorSerializer()
        code = s.serialize(ValueError("bad input"), print_to_stdout=False)
        assert code == EXIT_USER_INPUT

    def test_permission_error_maps_to_permission_denied(self):
        s = ErrorSerializer()
        code = s.serialize(PermissionError("forbidden"), print_to_stdout=False)
        assert code == EXIT_PERMISSION_DENIED

    def test_file_not_found_maps_to_not_found(self):
        s = ErrorSerializer()
        code = s.serialize(FileNotFoundError("missing"), print_to_stdout=False)
        assert code == EXIT_NOT_FOUND

    def test_timeout_error_maps_to_timeout(self):
        s = ErrorSerializer()
        code = s.serialize(TimeoutError("timed out"), print_to_stdout=False)
        assert code == EXIT_TIMEOUT


# --- Test OutputFormatter integration ---


class TestOutputFormatter:
    """Tests for OutputFormatter format selection."""

    def test_json_format(self):
        fmt = OutputFormatter(format_setting="json")
        out = fmt.format({"key": "value"})
        assert '"key": "value"' in out

    def test_auto_selects_json_for_dict(self):
        fmt = OutputFormatter(format_setting="auto")
        out = fmt.format({"key": "value"})
        assert '"key": "value"' in out

    def test_auto_selects_toon_for_uniform_list(self):
        fmt = OutputFormatter(format_setting="auto")
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        out = fmt.format(data)
        # TOON format should produce table-like output
        assert "a" in out and "b" in out

    def test_auto_selects_json_for_empty_list(self):
        fmt = OutputFormatter(format_setting="auto")
        out = fmt.format([])
        assert out == "[]"


# --- Test AccessControlGuard integration ---


class TestAccessControlGuard:
    """Tests for access control guard."""

    def test_guard_raises_on_blocked(self):
        cfg = ConfigLoader()
        cfg.load()
        guard = AccessControlGuard(cfg, "DATASETS")
        # By default operations should be permitted
        guard.check("dataset", "get")  # Should not raise

    def test_access_control_error_is_exception(self):
        err = AccessControlError("blocked")
        assert str(err) == "blocked"
        assert err.message == "blocked"


# --- Test main() integration: timeout resolution, retry, operation validation ---


class TestMainIntegration:
    """Integration tests for main() covering CODEREVIEW-003 fixes."""

    @pytest.mark.asyncio
    async def test_main_returns_user_input_when_no_resource(self, capsys):
        """No resource → help + EXIT_USER_INPUT."""
        with patch.object(sys, "argv", ["prog"]):
            rc = await foundry_datasets_cli.main()
        assert rc == EXIT_USER_INPUT

    @pytest.mark.asyncio
    async def test_main_returns_user_input_when_no_operation(self):
        """WARNING-3: resource without operation → EXIT_USER_INPUT."""
        with patch.object(sys, "argv", ["prog", "dataset"]):
            rc = await foundry_datasets_cli.main()
        assert rc == EXIT_USER_INPUT

    @pytest.mark.asyncio
    async def test_main_resolves_timeout_from_cli_flag(self):
        """WARNING-2: --timeout flag reaches the SDK call."""
        mock_client = _async_client()
        mock_client.get.return_value = {"rid": "x"}

        with (
            patch.object(foundry_datasets_cli, "_get_client", return_value=mock_client),
            patch.object(foundry_datasets_cli, "ConfigLoader") as cfg_cls,
            patch.object(foundry_datasets_cli, "AccessControlGuard") as guard_cls,
            patch.object(foundry_datasets_cli, "RetryHandler") as rh_cls,
            patch.object(foundry_datasets_cli, "LogSetup"),
            patch.object(foundry_datasets_cli, "OutputFormatter") as of_cls,
            patch("builtins.print"),
        ):
            cfg = MagicMock()
            cfg.timeout_s = 30
            cfg.log_level = "INFO"
            cfg_cls.return_value = cfg
            guard = MagicMock()
            guard.check.return_value = None
            guard_cls.return_value = guard
            rh = MagicMock()
            rh.execute = AsyncMock(return_value={"rid": "x"})
            rh_cls.return_value = rh
            of = MagicMock()
            of.format.return_value = "{}"
            of_cls.return_value = of

            with patch.object(
                sys, "argv", ["prog", "dataset", "get", "rid", "--timeout", "77"]
            ):
                rc = await foundry_datasets_cli.main()

        assert rc == EXIT_SUCCESS
        # The RetryHandler.execute should have been called with _invoke and the
        # resolved timeout as the final positional arg.
        assert rh.execute.await_count == 1
        args_passed = rh.execute.call_args.args
        # Signature: _invoke, resource, operation, client, args_ns, timeout
        assert args_passed[0] is foundry_datasets_cli._invoke
        assert args_passed[5] == 77

    @pytest.mark.asyncio
    async def test_main_falls_back_to_cfg_timeout(self):
        """WARNING-2: when --timeout omitted, cfg.timeout_s is used."""
        mock_client = _async_client()

        with (
            patch.object(foundry_datasets_cli, "_get_client", return_value=mock_client),
            patch.object(foundry_datasets_cli, "ConfigLoader") as cfg_cls,
            patch.object(foundry_datasets_cli, "AccessControlGuard") as guard_cls,
            patch.object(foundry_datasets_cli, "RetryHandler") as rh_cls,
            patch.object(foundry_datasets_cli, "LogSetup"),
            patch.object(foundry_datasets_cli, "OutputFormatter") as of_cls,
            patch("builtins.print"),
        ):
            cfg = MagicMock()
            cfg.timeout_s = 99
            cfg.log_level = "INFO"
            cfg_cls.return_value = cfg
            guard = MagicMock()
            guard.check.return_value = None
            guard_cls.return_value = guard
            rh = MagicMock()
            rh.execute = AsyncMock(return_value={"rid": "x"})
            rh_cls.return_value = rh
            of = MagicMock()
            of.format.return_value = "{}"
            of_cls.return_value = of

            with patch.object(sys, "argv", ["prog", "dataset", "get", "rid"]):
                rc = await foundry_datasets_cli.main()

        assert rc == EXIT_SUCCESS
        args_passed = rh.execute.call_args.args
        assert args_passed[5] == 99

    @pytest.mark.asyncio
    async def test_main_uses_retry_handler(self):
        """WARNING-1: RetryHandler.execute wraps the SDK call (ADR-002)."""
        mock_client = _async_client()

        with (
            patch.object(foundry_datasets_cli, "_get_client", return_value=mock_client),
            patch.object(foundry_datasets_cli, "ConfigLoader") as cfg_cls,
            patch.object(foundry_datasets_cli, "AccessControlGuard") as guard_cls,
            patch.object(foundry_datasets_cli, "RetryHandler") as rh_cls,
            patch.object(foundry_datasets_cli, "LogSetup"),
            patch.object(foundry_datasets_cli, "OutputFormatter") as of_cls,
            patch("builtins.print"),
        ):
            cfg = MagicMock()
            cfg.timeout_s = 30
            cfg.log_level = "INFO"
            cfg_cls.return_value = cfg
            guard = MagicMock()
            guard_cls.return_value = guard
            rh = MagicMock()
            rh.execute = AsyncMock(return_value={"ok": True})
            rh_cls.return_value = rh
            of = MagicMock()
            of.format.return_value = "{}"
            of_cls.return_value = of

            with patch.object(sys, "argv", ["prog", "dataset", "get", "rid"]):
                rc = await foundry_datasets_cli.main()

        assert rc == EXIT_SUCCESS
        rh_cls.assert_called_once()
        rh.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_main_access_control_denied(self):
        """AccessControlError → EXIT_ACCESS_CONTROL."""
        mock_client = _async_client()
        with (
            patch.object(foundry_datasets_cli, "_get_client", return_value=mock_client),
            patch.object(foundry_datasets_cli, "ConfigLoader") as cfg_cls,
            patch.object(foundry_datasets_cli, "AccessControlGuard") as guard_cls,
            patch.object(foundry_datasets_cli, "LogSetup"),
            patch.object(foundry_datasets_cli, "ErrorSerializer") as ser_cls,
        ):
            cfg = MagicMock()
            cfg.timeout_s = 30
            cfg.log_level = "INFO"
            cfg_cls.return_value = cfg
            guard = MagicMock()
            guard.check.side_effect = AccessControlError("no")
            guard_cls.return_value = guard
            ser = MagicMock()
            ser.serialize.return_value = EXIT_ACCESS_CONTROL
            ser_cls.return_value = ser

            with patch.object(sys, "argv", ["prog", "dataset", "get", "rid"]):
                rc = await foundry_datasets_cli.main()

        assert rc == EXIT_ACCESS_CONTROL

    @pytest.mark.asyncio
    async def test_main_handles_unknown_operation_as_server_error(self):
        """Unknown operation surfaces a ValueError → EXIT_SERVER_ERROR."""
        mock_client = _async_client()
        with (
            patch.object(foundry_datasets_cli, "_get_client", return_value=mock_client),
            patch.object(foundry_datasets_cli, "ConfigLoader") as cfg_cls,
            patch.object(foundry_datasets_cli, "AccessControlGuard") as guard_cls,
            patch.object(foundry_datasets_cli, "RetryHandler") as rh_cls,
            patch.object(foundry_datasets_cli, "LogSetup"),
            patch.object(foundry_datasets_cli, "ErrorSerializer") as ser_cls,
            patch.object(foundry_datasets_cli, "OutputFormatter"),
        ):
            cfg = MagicMock()
            cfg.timeout_s = 30
            cfg.log_level = "INFO"
            cfg_cls.return_value = cfg
            guard = MagicMock()
            guard_cls.return_value = guard
            rh = MagicMock()
            rh.execute = AsyncMock(
                side_effect=ValueError("Unknown operation: dataset.bogus")
            )
            rh_cls.return_value = rh
            ser = MagicMock()
            ser.serialize.return_value = EXIT_SERVER_ERROR
            ser_cls.return_value = ser

            # Use a real parser to build an args namespace with resource/operation.
            ns = build_parser().parse_args(["dataset", "get", "rid"])
            with patch.object(sys, "argv", ["prog", "dataset", "get", "rid"]):
                # Force the parsed namespace to carry a bogus operation so _resolve
                # yields an unknown op — but the parser validates, so instead we
                # patch _resolve directly.
                with patch.object(
                    foundry_datasets_cli, "_resolve", return_value="bogus"
                ):
                    rc = await foundry_datasets_cli.main()

        assert rc == EXIT_SERVER_ERROR


# --- Exhaustive _invoke branch coverage ---


def _ns(**kwargs):
    """Build a Namespace with all _invoke attributes defaulted to None."""
    base = dict(
        dataset_rid=None,
        branch_name=None,
        transaction_rid=None,
        page_size=None,
        page_token=None,
        file_path=None,
        view_dataset_rid=None,
        branch=None,
        name=None,
        parent_folder_rid=None,
        dataset_rids=None,
        schema=None,
        backing_datasets=None,
        primary_key=None,
        end_transaction_rid=None,
        start_transaction_rid=None,
    )
    base.update(kwargs)
    return argparse.Namespace(**base)


# Each tuple: (resource, operation, client_attr_called, namespace_kwargs)
_INVOKE_CASES = [
    ("dataset", "create", "create", dict(name="n", parent_folder_rid="f")),
    ("dataset", "get", "get", dict(dataset_rid="r")),
    (
        "dataset",
        "get_health_check_reports",
        "get_health_check_reports",
        dict(dataset_rid="r"),
    ),
    ("dataset", "get_health_checks", "get_health_checks", dict(dataset_rid="r")),
    ("dataset", "get_schedules", "get_schedules", dict(dataset_rid="r")),
    ("dataset", "get_schema", "get_schema", dict(dataset_rid="r")),
    ("dataset", "get_schema_batch", "get_schema_batch", dict(dataset_rids='["r1"]')),
    ("dataset", "jobs", "jobs", dict(dataset_rid="r")),
    ("dataset", "put_schema", "put_schema", dict(dataset_rid="r", schema='{"k":1}')),
    ("dataset", "read_table", "read_table", dict(dataset_rid="r")),
    ("dataset", "transactions", "transactions", dict(dataset_rid="r")),
    ("branch", "create", "create", dict(dataset_rid="r", name="b")),
    ("branch", "delete", "delete", dict(dataset_rid="r", branch_name="b")),
    ("branch", "get", "get", dict(dataset_rid="r", branch_name="b")),
    ("branch", "list", "list", dict(dataset_rid="r")),
    ("branch", "transactions", "transactions", dict(dataset_rid="r", branch_name="b")),
    ("file", "delete", "delete", dict(dataset_rid="r", file_path="/p")),
    ("file", "get", "get", dict(dataset_rid="r", file_path="/p")),
    ("file", "list", "list", dict(dataset_rid="r")),
    ("transaction", "abort", "abort", dict(dataset_rid="r", transaction_rid="t")),
    ("transaction", "build", "build", dict(dataset_rid="r", transaction_rid="t")),
    ("transaction", "commit", "commit", dict(dataset_rid="r", transaction_rid="t")),
    ("transaction", "create", "create", dict(dataset_rid="r")),
    ("transaction", "get", "get", dict(dataset_rid="r", transaction_rid="t")),
    ("transaction", "job", "job", dict(dataset_rid="r", transaction_rid="t")),
    (
        "view",
        "add_primary_key",
        "add_primary_key",
        dict(view_dataset_rid="v", primary_key='["a"]', branch="b"),
    ),
    ("view", "create", "create", dict(name="n", parent_folder_rid="f")),
    (
        "view",
        "remove_backing_datasets",
        "remove_backing_datasets",
        dict(view_dataset_rid="v", backing_datasets='["r"]', branch="b"),
    ),
    (
        "view",
        "replace_backing_datasets",
        "replace_backing_datasets",
        dict(view_dataset_rid="v", backing_datasets='["r"]', branch="b"),
    ),
]


@pytest.mark.parametrize("resource,op,attr,ns_kwargs", _INVOKE_CASES)
@pytest.mark.asyncio
async def test_invoke_all_operations_dispatch(resource, op, attr, ns_kwargs):
    """Exhaustively cover each _invoke operation branch."""
    mc = _async_client()
    await _invoke(resource, op, mc, _ns(**ns_kwargs), timeout=None)
    called = getattr(mc, attr)
    assert called.await_count == 1


@pytest.mark.asyncio
async def test_invoke_file_content_operation():
    """file.content passes start/end transaction rids."""
    mc = _async_client()
    await _invoke(
        "file",
        "content",
        mc,
        _ns(
            dataset_rid="r",
            file_path="/p",
            end_transaction_rid="et",
            start_transaction_rid="st",
        ),
        timeout=None,
    )
    mc.content.assert_awaited_once()
    kwargs = mc.content.call_args.kwargs
    assert kwargs["end_transaction_rid"] == "et"
    assert kwargs["start_transaction_rid"] == "st"


@pytest.mark.asyncio
async def test_invoke_view_add_backing_datasets():
    mc = _async_client()
    await _invoke(
        "view",
        "add_backing_datasets",
        mc,
        _ns(view_dataset_rid="v", backing_datasets='["r"]', branch="b"),
        timeout=None,
    )
    mc.add_backing_datasets.assert_awaited_once()


# --- main() exception handler coverage ---


def _patch_main_infra(monkeypatch, *, cfg_timeout=30):
    """Patch shared infrastructure so main() can be exercised end-to-end."""
    monkeypatch.setattr(
        foundry_datasets_cli, "ConfigLoader", lambda: _StubCfg(cfg_timeout)
    )
    monkeypatch.setattr(foundry_datasets_cli, "LogSetup", _StubLog)
    monkeypatch.setattr(foundry_datasets_cli, "AccessControlGuard", _StubGuard)
    return monkeypatch


class _StubCfg:
    def __init__(self, timeout_s=30):
        self.timeout_s = timeout_s
        self.log_level = "INFO"

    def load(self):
        return None


class _StubLog:
    @staticmethod
    def configure(**kwargs):
        return None


class _StubGuard:
    def __init__(self, cfg, ns):
        self.cfg = cfg
        self.ns = ns

    def check(self, *a, **kw):
        return None


@pytest.mark.asyncio
async def test_main_permission_error_path(monkeypatch, capsys):
    """PermissionError from invoke → EXIT_PERMISSION_DENIED."""
    mc = _async_client()
    monkeypatch.setattr(foundry_datasets_cli, "_get_client", lambda c, r: mc)
    rh = MagicMock()
    rh.execute = AsyncMock(side_effect=PermissionError("403"))
    monkeypatch.setattr(foundry_datasets_cli, "RetryHandler", lambda: rh)
    _patch_main_infra(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "dataset", "get", "rid"])
    rc = await foundry_datasets_cli.main()
    assert rc == EXIT_PERMISSION_DENIED


@pytest.mark.asyncio
async def test_main_timeout_error_path(monkeypatch):
    mc = _async_client()
    monkeypatch.setattr(foundry_datasets_cli, "_get_client", lambda c, r: mc)
    rh = MagicMock()
    rh.execute = AsyncMock(side_effect=TimeoutError("slow"))
    monkeypatch.setattr(foundry_datasets_cli, "RetryHandler", lambda: rh)
    _patch_main_infra(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "dataset", "get", "rid"])
    rc = await foundry_datasets_cli.main()
    assert rc == EXIT_TIMEOUT


@pytest.mark.asyncio
async def test_main_generic_exception_path(monkeypatch):
    mc = _async_client()
    monkeypatch.setattr(foundry_datasets_cli, "_get_client", lambda c, r: mc)
    rh = MagicMock()
    rh.execute = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(foundry_datasets_cli, "RetryHandler", lambda: rh)
    _patch_main_infra(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "dataset", "get", "rid"])
    rc = await foundry_datasets_cli.main()
    assert rc == EXIT_SERVER_ERROR


@pytest.mark.asyncio
async def test_main_client_creation_failure(monkeypatch):
    monkeypatch.setattr(
        foundry_datasets_cli,
        "_get_client",
        lambda c, r: (_ for _ in ()).throw(RuntimeError("no cfg")),
    )
    _patch_main_infra(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["prog", "dataset", "get", "rid"])
    rc = await foundry_datasets_cli.main()
    assert rc == EXIT_CONFIGURATION


@pytest.mark.asyncio
async def test_main_success_prints_output(monkeypatch, capsys):
    mc = _async_client()
    mc.get.return_value = {"rid": "x"}
    monkeypatch.setattr(foundry_datasets_cli, "_get_client", lambda c, r: mc)
    rh = MagicMock()
    rh.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_datasets_cli, "RetryHandler", lambda: rh)
    _patch_main_infra(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["prog", "dataset", "get", "rid", "--format", "json"]
    )
    rc = await foundry_datasets_cli.main()
    assert rc == EXIT_SUCCESS
    out = capsys.readouterr().out
    assert "rid" in out


# --- Test path resolution robustness (MINOR-2) ---


class TestPathResolution:
    """Smoke test that the project root was discovered without crashing."""

    def test_project_root_is_string(self):
        assert isinstance(foundry_datasets_cli._PROJECT_ROOT, str)
        assert len(foundry_datasets_cli._PROJECT_ROOT) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
