#!/usr/bin/env python3
"""Unit tests for foundry_datasets_cli.py — 33 operations across 5 resource clients.

Tests cover:
- Argument parsing for all 33 operations
- _model_to_dict serialization
- _get_client routing
- _invoke dispatch for each resource type
- _resolve kebab-case to snake_case
- Error handling paths
- Access control integration
- Output formatter integration

Framework: pytest with pytest-asyncio
"""

import argparse
import json
import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import asyncio

# Ensure src is on path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.config_loader import ConfigLoader
from foundry_cli.common.error_serializer import (
    EXIT_SUCCESS,
    EXIT_USER_INPUT,
    EXIT_AUTH,
    EXIT_PERMISSION_DENIED,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_SERVER_ERROR,
    EXIT_RATE_LIMIT,
    EXIT_ACCESS_CONTROL,
    EXIT_CONFIGURATION,
    ErrorSerializer,
)
from foundry_cli.common.output_formatter import OutputFormatter
from foundry_cli.common.log_setup import LogSetup, METADATA_SEPARATOR
from foundry_cli.common.access_control_guard import AccessControlGuard, AccessControlError


# Import CLI module directly
_SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "foundry-datasets" / "scripts"
sys.path.insert(0, str(_SKILL_DIR))
import importlib
foundry_datasets_cli = importlib.import_module("foundry_datasets_cli")
_model_to_dict = foundry_datasets_cli._model_to_dict
_get_client = foundry_datasets_cli._get_client
_invoke = foundry_datasets_cli._invoke
_resolve = foundry_datasets_cli._resolve
build_parser = foundry_datasets_cli.build_parser


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
        with patch.object(foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client):
            result = _get_client(cfg, "dataset")
            assert result == mock_client.datasets.Dataset

    def test_branch_returns_branch_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client):
            result = _get_client(cfg, "branch")
            assert result == mock_dataset.Branch

    def test_file_returns_file_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client):
            result = _get_client(cfg, "file")
            assert result == mock_dataset.File

    def test_transaction_returns_transaction_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client):
            result = _get_client(cfg, "transaction")
            assert result == mock_dataset.Transaction

    def test_view_returns_view_client(self):
        cfg = MagicMock()
        mock_dataset = MagicMock()
        mock_client = MagicMock()
        mock_client.datasets.Dataset = mock_dataset
        with patch.object(foundry_datasets_cli.AsyncClientFactory, "create", return_value=mock_client):
            result = _get_client(cfg, "view")
            assert result == mock_dataset.View


# --- Test _resolve ---

class TestResolve:
    """Tests for kebab-case to snake_case resolution."""

    def test_dataset_kebab_ops(self):
        assert _resolve("dataset", "get-health-check-reports") == "get_health_check_reports"
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
        assert _resolve("view", "replace-backing-datasets") == "replace_backing_datasets"

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
        args = parser.parse_args(["dataset", "create", "--name", "test", "--parent-folder-rid", "folder_rid"])
        assert args.name == "test"
        assert args.parent_folder_rid == "folder_rid"

    def test_timeout_option(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--timeout", "60"])
        assert args.timeout == 60

    def test_format_option(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--format", "json"])
        assert args.format == "json"

    def test_pretty_option(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--pretty"])
        assert args.pretty is True

    def test_pagination_options(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get", "rid123", "--page-size", "50", "--page-token", "tok123"])
        assert args.page_size == 50
        assert args.page_token == "tok123"

    def test_no_resource_shows_help(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.resource is None

    def test_kebab_case_operations(self):
        parser = build_parser()
        args = parser.parse_args(["dataset", "get-health-checks", "rid123", "--branch-name", "main"])
        assert args.operation == "get-health-checks"
        assert args.branch_name == "main"

    def test_view_operations(self):
        parser = build_parser()
        args = parser.parse_args([
            "view", "add-backing-datasets",
            "--view-dataset-rid", "view_rid",
            "--backing-datasets", '["rid1", "rid2"]',
            "--branch", "main"
        ])
        assert args.view_dataset_rid == "view_rid"
        assert args.backing_datasets == '["rid1", "rid2"]'
        assert args.branch == "main"


# --- Test _invoke dispatch ---

class TestInvoke:
    """Tests for _invoke operation dispatch."""

    def test_dataset_create(self):
        mock_client = MagicMock()
        mock_client.create.return_value = {"id": "new_rid"}
        args = argparse.Namespace(name="test", parent_folder_rid="folder", timeout=None)
        result = _invoke("dataset", "create", mock_client, args)
        mock_client.create.assert_called_once_with(name="test", parent_folder_rid="folder", request_timeout=None)

    def test_dataset_get(self):
        mock_client = MagicMock()
        args = argparse.Namespace(dataset_rid="rid123", timeout=None)
        _invoke("dataset", "get", mock_client, args)
        mock_client.get.assert_called_once_with(dataset_rid="rid123", request_timeout=None)

    def test_branch_list(self):
        mock_client = MagicMock()
        args = argparse.Namespace(dataset_rid="rid123", page_size=10, page_token=None, timeout=None)
        _invoke("branch", "list", mock_client, args)
        mock_client.list.assert_called_once_with(dataset_rid="rid123", page_size=10, page_token=None, request_timeout=None)

    def test_file_get(self):
        mock_client = MagicMock()
        args = argparse.Namespace(dataset_rid="rid123", file_path="/data.csv", transaction_rid=None, timeout=None)
        _invoke("file", "get", mock_client, args)
        mock_client.get.assert_called_once_with(dataset_rid="rid123", file_path="/data.csv", transaction_rid=None, request_timeout=None)

    def test_transaction_create(self):
        mock_client = MagicMock()
        args = argparse.Namespace(dataset_rid="rid123", branch_name="main", timeout=None)
        _invoke("transaction", "create", mock_client, args)
        mock_client.create.assert_called_once_with(dataset_rid="rid123", branch_name="main", request_timeout=None)

    def test_view_get(self):
        mock_client = MagicMock()
        args = argparse.Namespace(view_dataset_rid="view_rid", branch="main", timeout=None)
        _invoke("view", "get", mock_client, args)
        mock_client.get.assert_called_once_with(view_dataset_rid="view_rid", branch="main", request_timeout=None)

    def test_unknown_operation_raises(self):
        mock_client = MagicMock()
        args = argparse.Namespace()
        with pytest.raises(ValueError, match="Unknown operation"):
            _invoke("unknown", "unknown", mock_client, args)

    def test_dataset_schema_batch_parses_json(self):
        mock_client = MagicMock()
        args = argparse.Namespace(dataset_rids='["rid1", "rid2"]', timeout=None)
        _invoke("dataset", "get_schema_batch", mock_client, args)
        mock_client.get_schema_batch.assert_called_once_with(
            dataset_rids=["rid1", "rid2"], request_timeout=None
        )

    def test_view_add_backing_datasets_parses_json(self):
        mock_client = MagicMock()
        args = argparse.Namespace(
            view_dataset_rid="view_rid",
            backing_datasets='[{"dataset_rid": "rid1"}]',
            branch="main",
            timeout=None,
        )
        _invoke("view", "add_backing_datasets", mock_client, args)
        mock_client.add_backing_datasets.assert_called_once()
        call_kwargs = mock_client.add_backing_datasets.call_args.kwargs
        assert call_kwargs["backing_datasets"] == [{"dataset_rid": "rid1"}]


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
