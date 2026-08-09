#!/usr/bin/env python3
"""Foundry Functions CLI - 7 canonical API v2 operations."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT: str | None = None
_candidate = _SCRIPT_DIR
for _depth in range(8):
    if (_candidate / "src" / "foundry_cli" / "__init__.py").exists():
        _PROJECT_ROOT = str(_candidate)
        break
    if _candidate.parent == _candidate:
        break
    _candidate = _candidate.parent
if _PROJECT_ROOT is None:
    _PROJECT_ROOT = str(_SCRIPT_DIR.parents[4])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from foundry_cli.common.access_control_guard import (  # noqa: E402
    AccessControlError,
    AccessControlGuard,
)
from foundry_cli.common.async_client_factory import AsyncClientFactory  # noqa: E402
from foundry_cli.common.config_loader import ConfigLoader  # noqa: E402
from foundry_cli.common.error_serializer import (  # noqa: E402
    EXIT_ACCESS_CONTROL,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION_DENIED,
    EXIT_RATE_LIMIT,
    EXIT_SERVER_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_INPUT,
    ErrorSerializer,
)
from foundry_cli.common.log_setup import LogSetup  # noqa: E402
from foundry_cli.common.output_formatter import OutputFormatter  # noqa: E402
from foundry_cli.common.retry import RetryHandler  # noqa: E402

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]


def _op(
    resource: str,
    operation: str,
    client_path: str,
    method: str,
    positional: Iterable[str] = (),
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    *,
    json_args: Iterable[str] = (),
) -> OperationSpec:
    """Build an operation specification entry."""
    return {
        "resource": resource,
        "operation": operation,
        "client_path": client_path,
        "method": method,
        "positional": tuple(positional),
        "required": tuple(required),
        "optional": tuple(optional),
        "json_args": frozenset(json_args),
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op(
        "query",
        "execute",
        "Query",
        "execute",
        ("query_api_name",),
        required=("parameters",),
        optional=(
            "attribution",
            "branch",
            "preview",
            "trace_parent",
            "trace_state",
            "transaction_id",
            "version",
        ),
        json_args=("parameters", "attribution"),
    ),
    _op(
        "query",
        "get",
        "Query",
        "get",
        ("query_api_name",),
        optional=("preview", "version"),
    ),
    _op(
        "query",
        "get_by_rid",
        "Query",
        "get_by_rid",
        required=("rid",),
        optional=("include_prerelease", "preview", "version"),
    ),
    _op(
        "query",
        "get_by_rid_batch",
        "Query",
        "get_by_rid_batch",
        ("body",),
        optional=("preview",),
        json_args=("body",),
    ),
    _op(
        "query",
        "streaming_execute",
        "Query",
        "streaming_execute",
        ("query_api_name",),
        required=("parameters",),
        optional=(
            "attribution",
            "branch",
            "ontology",
            "preview",
            "trace_parent",
            "trace_state",
            "transaction_id",
            "version",
        ),
        json_args=("parameters", "attribution"),
    ),
    _op("value_type", "get", "ValueType", "get", ("value_type_rid",), optional=("preview",)),
    _op(
        "version_id",
        "get",
        "ValueType.VersionId",
        "get",
        ("value_type_rid", "version_id_version_id"),
        optional=("preview",),
    ),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_spec["resource"], {})[_spec["operation"]] = _spec

PAGINATED_OPS: frozenset[tuple[str, str]] = frozenset()


def _model_to_dict(obj: Any) -> Any:
    """Convert SDK/Pydantic objects into JSON-serializable values."""
    if obj is None:
        return None
    if isinstance(obj, (bytes, bytearray)):
        return {"bytes": len(obj)}
    if isinstance(obj, list):
        return [_model_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _model_to_dict(value) for key, value in obj.items()}
    try:
        result = obj.to_dict()
        if isinstance(result, dict):
            return result
    except (AttributeError, TypeError):
        pass
    try:
        result = obj.model_dump()
        if isinstance(result, dict):
            return result
    except (AttributeError, TypeError):
        pass
    try:
        result = obj.dict()
        if isinstance(result, dict):
            return result
    except (AttributeError, TypeError):
        pass
    return obj


def _resolve(resource: str, op: str) -> str:
    """Resolve CLI kebab-case operation name to SDK snake_case."""
    return op.replace("-", "_")


def _kebab(name: str) -> str:
    """Convert snake_case names to CLI kebab-case."""
    return name.replace("_", "-")


def _common_parser() -> argparse.ArgumentParser:
    """Build shared parser for operation-level common options."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=None)
    common.add_argument("--format", choices=["json", "toon", "auto"], default="auto")
    common.add_argument("--pretty", action="store_true")
    return common


def _add_kwarg(
    parser: argparse.ArgumentParser, arg_name: str, *, required: bool
) -> None:
    flag = "--" + _kebab(arg_name)
    if arg_name in {"include_prerelease", "preview"}:
        parser.add_argument(flag, action="store_true", default=None, dest=arg_name)
        return
    parser.add_argument(flag, required=required, default=None, dest=arg_name)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 7 functions operations."""
    parser = argparse.ArgumentParser(
        prog="foundry_functions_cli",
        description="Foundry Functions CLI - 7 operations",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    for resource in sorted(OPERATION_BY_RESOURCE):
        res_parser = subparsers.add_parser(_kebab(resource))
        op_sub = res_parser.add_subparsers(dest="operation")
        for operation, spec in sorted(OPERATION_BY_RESOURCE[resource].items()):
            op_parser = op_sub.add_parser(_kebab(operation), parents=[_common_parser()])
            for arg_name in spec["positional"]:
                op_parser.add_argument(arg_name)
            for arg_name in spec["required"]:
                _add_kwarg(op_parser, arg_name, required=True)
            for arg_name in spec["optional"]:
                _add_kwarg(op_parser, arg_name, required=False)

    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return the operation spec for a resource and operation."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(
    cfg: ConfigLoader, resource: str, factory: AsyncClientFactory | None = None
) -> Any:
    """Get SDK client for a functions resource."""
    root = (factory or AsyncClientFactory()).create(cfg).functions
    spec = next(iter(OPERATION_BY_RESOURCE[resource].values()))
    client = root
    for attr in spec["client_path"].split("."):
        client = getattr(client, attr)
    return client


def _coerce_arg(value: Any, *, json_arg: bool) -> Any:
    """Parse JSON CLI values for structured SDK arguments."""
    if value is None:
        return None
    if json_arg and isinstance(value, str):
        return json.loads(value)
    return value


async def _resolve_result(value: Any) -> Any:
    """Await SDK calls that return awaitables."""
    if asyncio.isfuture(value) or inspect.isawaitable(value):
        return await value
    return value


async def _invoke(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
) -> Any:
    """Invoke a functions SDK operation."""
    spec = _spec_for(resource, operation)
    method = getattr(client, spec["method"])
    positional = [
        _coerce_arg(getattr(args, name), json_arg=name in spec["json_args"])
        for name in spec["positional"]
    ]
    kwargs: dict[str, Any] = {}

    for name in (*spec["required"], *spec["optional"]):
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = _coerce_arg(value, json_arg=name in spec["json_args"])

    kwargs["request_timeout"] = timeout
    return await _resolve_result(method(*positional, **kwargs))


async def main() -> int:
    """Run the Foundry Functions CLI."""
    parser = build_parser()
    args = parser.parse_args()
    if not args.resource:
        parser.print_help()
        return EXIT_USER_INPUT
    if not getattr(args, "operation", None):
        parser.print_help()
        return EXIT_USER_INPUT

    cfg = ConfigLoader()
    cfg.load()
    LogSetup.configure(log_level=cfg.log_level)

    resource = args.resource.replace("-", "_")
    operation = _resolve(resource, args.operation)
    logger.info(
        "Executing operation", extra={"resource": resource, "operation": operation}
    )

    try:
        _spec_for(resource, operation)
    except ValueError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_USER_INPUT

    try:
        AccessControlGuard(cfg, "FUNCTIONS").check(resource, operation)
    except AccessControlError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_ACCESS_CONTROL

    factory = AsyncClientFactory()
    try:
        with factory.invocation_scope(cfg):
            client = _get_client(cfg, resource, factory)
            timeout = getattr(args, "timeout", None) or getattr(cfg, "timeout_s", None)
            retry_handler = RetryHandler()
            result = await retry_handler.execute(
                _invoke,
                resource,
                operation,
                client,
                args,
                timeout,
            )
        formatter = OutputFormatter(
            format_setting=getattr(args, "format", "auto"),
            pretty=getattr(args, "pretty", False),
        )
        print(formatter.format(_model_to_dict(result)))
        return EXIT_SUCCESS
    except PermissionError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_PERMISSION_DENIED
    except FileNotFoundError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_NOT_FOUND
    except TimeoutError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_TIMEOUT
    except ValueError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_USER_INPUT
    except OSError as exc:
        exit_code = ErrorSerializer().serialize(exc)
        if getattr(exc, "errno", None) in (11, 115):
            return EXIT_RATE_LIMIT
        return exit_code
    except Exception as exc:
        exit_code = ErrorSerializer().serialize(exc)
        if exit_code != EXIT_USER_INPUT:
            return exit_code
        return EXIT_SERVER_ERROR


def console_main() -> int:
    """Run the async CLI from the console script entry point."""
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(console_main())
