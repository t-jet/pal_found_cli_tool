#!/usr/bin/env python3
"""Foundry Streams CLI - 15 canonical Streams API v2 operations.

Exposes exactly 15 public ``foundry_sdk.v2.streams`` operations as CLI
subcommands across the Dataset, Stream, and Subscriber client paths:

- dataset (1): create
- stream (7): create, get, get-end-offsets, get-records,
  publish-binary-record, publish-record, publish-records, reset
- subscriber (7): create, commit-offsets, delete, get-read-position,
  read-records, reset-offsets

Record-reading operations implement the ADR-003 batch-response pattern:
retrieve up to ``--max-records`` records then exit; no persistent streaming
or progressive stdout emission.

Usage: foundry-streams <resource> <operation> [options]
Output: JSON/TOON on stdout, NDJSON diagnostics on stderr (ADR-004, ADR-005).
Exit codes per ADR-001 taxonomy.
Access control: AccessControlGuard before client construction and file
effects (SRS 4.2, ADR-007). Retry: transient conditions only per ADR-002.
Tracing: SDK-native B3 via invocation_scope; include_attribution=False.
Timeouts: FOUNDRY_AGENTIC_CLI_STREAMS_TIMEOUT_S (default 120s) per ADR-003.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, NoReturn

from foundry_cli.common.access_control_guard import (
    AccessControlError,
    AccessControlGuard,
)
from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.common.config_loader import ConfigLoader, ConfigurationError
from foundry_cli.common.error_serializer import (
    EXIT_ACCESS_CONTROL,
    EXIT_AUTH,
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
from foundry_cli.common.log_setup import LogSetup
from foundry_cli.common.output_formatter import OutputFormatter
from foundry_cli.common.retry import RetryHandler
from foundry_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]

_METADATA_ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[1] / "metadata-allow-list.md"
)

# Streams namespace per-call timeout (ADR-002/ADR-003).
_STREAMS_TIMEOUT_ENV = "FOUNDRY_AGENTIC_CLI_STREAMS_TIMEOUT_S"
_STREAMS_DEFAULT_TIMEOUT_S = 120

# ADR-003 batch-read bounds.
_GET_RECORDS_MAX_RECORDS = 10_000
_READ_RECORDS_MAX_RECORDS = 1_000
_DEFAULT_MAX_RECORDS = 100


class CLIInputError(ValueError):
    """A locally generated input error whose message never echoes values."""

    exit_code = EXIT_USER_INPUT


class _ArgumentParser(argparse.ArgumentParser):
    """Raise parser failures into the structured error boundary."""

    def error(self, message: str) -> NoReturn:
        raise CLIInputError("Invalid command input")


def _op(
    resource: str,
    operation: str,
    client_path: tuple[str, ...],
    method: str,
    positional: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    *,
    json_args: frozenset[str] = frozenset(),
    list_json_args: frozenset[str] = frozenset(),
) -> OperationSpec:
    """Build one operation specification entry."""
    return {
        "resource": resource,
        "operation": operation,
        "client_path": client_path,
        "method": method,
        "positional": positional,
        "required": required,
        "optional": optional,
        "json_args": json_args,
        "list_json_args": list_json_args,
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op(
        "dataset",
        "create",
        ("Dataset",),
        "create",
        required=("name", "parent_folder_rid", "schema"),
        optional=("branch_name", "compressed", "partitions_count", "stream_type"),
        json_args=frozenset({"schema"}),
    ),
    _op(
        "stream",
        "create",
        ("Dataset", "Stream"),
        "create",
        ("dataset_rid",),
        required=("branch_name", "schema"),
        optional=("compressed", "partitions_count", "stream_type"),
        json_args=frozenset({"schema"}),
    ),
    _op(
        "stream",
        "get",
        ("Dataset", "Stream"),
        "get",
        ("dataset_rid", "stream_branch_name"),
    ),
    _op(
        "stream",
        "get_end_offsets",
        ("Dataset", "Stream"),
        "get_end_offsets",
        ("dataset_rid", "stream_branch_name"),
        optional=("view_rid",),
    ),
    _op(
        "stream",
        "get_records",
        ("Dataset", "Stream"),
        "get_records",
        ("dataset_rid", "stream_branch_name"),
        required=("partition_id",),
        optional=("start_offset", "view_rid"),
    ),
    _op(
        "stream",
        "publish_binary_record",
        ("Dataset", "Stream"),
        "publish_binary_record",
        ("dataset_rid", "stream_branch_name"),
        required=("file",),
        optional=("view_rid",),
    ),
    _op(
        "stream",
        "publish_record",
        ("Dataset", "Stream"),
        "publish_record",
        ("dataset_rid", "stream_branch_name"),
        required=("record",),
        optional=("view_rid",),
        json_args=frozenset({"record"}),
    ),
    _op(
        "stream",
        "publish_records",
        ("Dataset", "Stream"),
        "publish_records",
        ("dataset_rid", "stream_branch_name"),
        required=("records",),
        optional=("view_rid",),
        list_json_args=frozenset({"records"}),
    ),
    _op(
        "stream",
        "reset",
        ("Dataset", "Stream"),
        "reset",
        ("dataset_rid", "stream_branch_name"),
        optional=("schema", "compressed", "partitions_count", "stream_type"),
        json_args=frozenset({"schema"}),
    ),
    _op(
        "subscriber",
        "create",
        ("Dataset", "Stream", "Subscriber"),
        "create",
        ("dataset_rid", "stream_branch_name"),
        required=("subscriber_id",),
        optional=("read_position",),
        json_args=frozenset({"read_position"}),
    ),
    _op(
        "subscriber",
        "commit_offsets",
        ("Dataset", "Stream", "Subscriber"),
        "commit_offsets",
        ("dataset_rid", "stream_branch_name", "subscriber_subscriber_id"),
        required=("offsets",),
        optional=("view_rid",),
        json_args=frozenset({"offsets"}),
    ),
    _op(
        "subscriber",
        "delete",
        ("Dataset", "Stream", "Subscriber"),
        "delete",
        ("dataset_rid", "stream_branch_name", "subscriber_subscriber_id"),
    ),
    _op(
        "subscriber",
        "get_read_position",
        ("Dataset", "Stream", "Subscriber"),
        "get_read_position",
        ("dataset_rid", "stream_branch_name", "subscriber_subscriber_id"),
        optional=("view_rid",),
    ),
    _op(
        "subscriber",
        "read_records",
        ("Dataset", "Stream", "Subscriber"),
        "read_records",
        ("dataset_rid", "stream_branch_name", "subscriber_subscriber_id"),
        optional=("auto_commit", "partition_ids", "view_rid"),
        list_json_args=frozenset({"partition_ids"}),
    ),
    _op(
        "subscriber",
        "reset_offsets",
        ("Dataset", "Stream", "Subscriber"),
        "reset_offsets",
        ("dataset_rid", "stream_branch_name", "subscriber_subscriber_id"),
        required=("position",),
        json_args=frozenset({"position"}),
    ),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_spec["resource"], {})[_spec["operation"]] = _spec

# Structured JSON arguments across the catalog use the ``-json`` flag suffix.
_JSON_ARG_NAMES: frozenset[str] = frozenset(
    name
    for spec in OP_SPECS
    for name in (*spec["json_args"], *spec["list_json_args"])
)

# Integer-typed SDK options.
_INT_ARG_NAMES: frozenset[str] = frozenset({"partitions_count"})

# Boolean-typed SDK options registered as store_true flags.
_BOOLEAN_ARG_NAMES: frozenset[str] = frozenset({"compressed", "auto_commit"})

# Batch-read commands: (resource, operation) -> max allowed records.
BATCH_READ_OPS: dict[tuple[str, str], int] = {
    ("stream", "get_records"): _GET_RECORDS_MAX_RECORDS,
    ("subscriber", "read_records"): _READ_RECORDS_MAX_RECORDS,
}


def _common_parser() -> _ArgumentParser:
    """Build operation-level common options."""
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--format", choices=("json", "toon", "auto"), default="auto")
    parser.add_argument("--pretty", action="store_true")
    return parser


def _kebab(name: str) -> str:
    """Convert snake_case names to CLI kebab-case."""
    return name.replace("_", "-")


def _add_kwarg(
    parser: argparse.ArgumentParser, arg_name: str, *, required: bool
) -> None:
    """Add one named operation argument."""
    if arg_name in _JSON_ARG_NAMES:
        flag = "--" + _kebab(arg_name) + "-json"
    elif arg_name in _BOOLEAN_ARG_NAMES:
        flag = "--" + _kebab(arg_name)
        parser.add_argument(flag, action="store_true", default=None, dest=arg_name)
        return
    else:
        flag = "--" + _kebab(arg_name)
    kwargs: dict[str, Any] = {"required": required, "default": None, "dest": arg_name}
    if arg_name in _INT_ARG_NAMES:
        kwargs["type"] = int
    parser.add_argument(flag, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 15 Streams operations."""
    parser = _ArgumentParser(
        prog="foundry-streams",
        description="Foundry Streams CLI - 15 Streams API v2 operations",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    for resource in sorted(OPERATION_BY_RESOURCE):
        res_parser = subparsers.add_parser(_kebab(resource))
        op_sub = res_parser.add_subparsers(dest="operation")
        for operation, spec in sorted(OPERATION_BY_RESOURCE[resource].items()):
            op_parser = op_sub.add_parser(
                _kebab(operation), parents=[_common_parser()]
            )
            for arg_name in spec["positional"]:
                op_parser.add_argument(arg_name)
            for arg_name in spec["required"]:
                _add_kwarg(op_parser, arg_name, required=True)
            for arg_name in spec["optional"]:
                _add_kwarg(op_parser, arg_name, required=False)
            if (resource, operation) in BATCH_READ_OPS:
                op_parser.add_argument(
                    "--max-records",
                    type=int,
                    default=_DEFAULT_MAX_RECORDS,
                    dest="max_records",
                    help=(
                        "Maximum number of records to read in one batch "
                        "(ADR-003)."
                    ),
                )

    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return one catalog operation specification."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise CLIInputError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(root_client: Any, client_path: tuple[str, ...]) -> Any:
    """Resolve the exact public nested Streams SDK resource."""
    client = root_client.streams
    for attribute in client_path:
        client = getattr(client, attribute)
    return client


def _required_text(value: Any, *, field: str) -> str:
    """Validate a non-empty string without echoing its value."""
    if not isinstance(value, str) or not value.strip():
        raise CLIInputError(f"{field} must not be empty")
    return value


def _decode_json(value: str, *, field: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise CLIInputError(f"{field} must contain valid JSON") from exc


def _parse_json_object(value: str, *, field: str) -> dict[str, Any]:
    """Decode one JSON object."""
    result = _decode_json(value, field=field)
    if not isinstance(result, dict):
        raise CLIInputError(f"{field} must be a JSON object")
    return result


def _parse_json_list(value: str, *, field: str) -> list[Any]:
    """Decode one JSON array."""
    result = _decode_json(value, field=field)
    if not isinstance(result, list):
        raise CLIInputError(f"{field} must be a JSON array")
    return result


def _validate_timeout(value: int) -> int:
    """Validate the effective per-attempt timeout from ADR-002."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise CLIInputError("timeout must be between 1 and 3600 seconds")
    return value


def _read_max_records(args: argparse.Namespace, spec: OperationSpec) -> int:
    """Validate and return the effective batch size for a batch-read op."""
    maximum = BATCH_READ_OPS[(spec["resource"], spec["operation"])]
    value = int(getattr(args, "max_records", _DEFAULT_MAX_RECORDS))
    if not 1 <= value <= maximum:
        raise CLIInputError(
            f"max-records must be between 1 and {maximum} for "
            f"{spec['resource']} {spec['operation']}"
        )
    return value


def _read_file_bounded(path_value: str, *, field: str) -> bytes:
    """Read a local file for binary publish with a hard size bound."""
    path = Path(path_value)
    if not path.is_file():
        raise CLIInputError(f"{field} must reference an existing file")
    maximum = 16 * 1024 * 1024  # 16 MiB publish cap (aligned with FR-DL).
    size = path.stat().st_size
    if size > maximum:
        raise CLIInputError(f"{field} exceeds the maximum publish size")
    return path.read_bytes()


def _validate_inputs(spec: OperationSpec, args: argparse.Namespace) -> None:
    """Validate scalars and decode JSON arguments before client creation.

    Local validation that runs before any access-control or client work:
    scalar checks, JSON decoding, and batch-read bounds (ADR-003). The
    bounded binary-file read for ``publish_binary_record`` happens after
    the access-control decision (DESIGN-016) and before client creation.
    """
    for name in (*spec["positional"], *spec["required"]):
        value = getattr(args, name, None)
        if isinstance(value, str):
            _required_text(value, field=name)
    for name in spec["json_args"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_object(value, field=name))
    for name in spec["list_json_args"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_list(value, field=name))
    if (spec["resource"], spec["operation"]) in BATCH_READ_OPS:
        _read_max_records(args, spec)


def _model_to_dict(value: Any) -> Any:
    """Convert SDK models to JSON-compatible values."""
    if value is None:
        return None
    if isinstance(value, list):
        return [_model_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _model_to_dict(item) for key, item in value.items()}
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, dict):
                return {key: _model_to_dict(item) for key, item in result.items()}
    return value


def _build_kwargs(
    spec: OperationSpec, args: argparse.Namespace, timeout: int
) -> dict[str, Any]:
    """Build SDK keyword arguments, omitting absent optional values.

    Batch-read commands map ``--max-records`` onto the SDK ``limit``
    argument (ADR-003). ``--file`` on publish_binary_record is consumed
    separately and never forwarded.
    """
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        if name == "file":
            continue
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = value
    if (spec["resource"], spec["operation"]) in BATCH_READ_OPS:
        kwargs["limit"] = _read_max_records(args, spec)
    kwargs["request_timeout"] = timeout
    return kwargs


async def _invoke(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
) -> Any:
    """Invoke one SDK operation with bounded local validation first."""
    positional = [getattr(args, name) for name in spec["positional"]]
    if spec["method"] == "publish_binary_record":
        positional.append(getattr(args, "_file_bytes"))
    return await getattr(client, spec["method"])(
        *positional, **_build_kwargs(spec, args, timeout)
    )


def _serialize_error(exception: BaseException) -> int:
    """Write one privacy-safe ADR-001 error envelope to stdout."""
    serializer = ErrorSerializer()
    status = sdk_http_status(exception)
    declared = getattr(exception, "exit_code", None)
    sdk_exit = sdk_exception_exit_code(exception)
    if isinstance(declared, int) and 1 <= declared <= 9:
        exit_code = declared
    elif sdk_exit is not None:
        exit_code = sdk_exit
    elif status == 401:
        exit_code = EXIT_AUTH
    elif status == 403:
        exit_code = EXIT_PERMISSION_DENIED
    elif status == 404:
        exit_code = EXIT_NOT_FOUND
    elif status == 429:
        exit_code = EXIT_RATE_LIMIT
    elif isinstance(exception, ConfigurationError):
        exit_code = EXIT_CONFIGURATION
    elif isinstance(exception, (TypeError, ValueError)):
        exit_code = EXIT_USER_INPUT
    elif isinstance(exception, TimeoutError):
        exit_code = EXIT_TIMEOUT
    else:
        exit_code = EXIT_SERVER_ERROR

    safe = isinstance(
        exception,
        (CLIInputError, ConfigurationError, TimeoutError),
    ) or declared in {EXIT_ACCESS_CONTROL, EXIT_CONFIGURATION}
    message = str(exception) if safe else "Streams operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if isinstance(status, int):
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "Streams operation failed",
        extra={
            "call_id": serializer.call_id,
            "exception_type": type(exception).__name__,
            "http_status": status,
        },
    )
    return exit_code


async def main() -> int:
    """Run one stateless Foundry Streams invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("a Streams operation is required")
    except CLIInputError as exc:
        return _serialize_error(exc)

    try:
        cfg = ConfigLoader()
        cfg.load()
        LogSetup.configure(log_level=cfg.log_level)

        resource = args.resource.replace("-", "_")
        operation = args.operation.replace("-", "_")
        spec = _spec_for(resource, operation)
        _validate_inputs(spec, args)
        default_timeout = cfg.get_int(_STREAMS_TIMEOUT_ENV, None)
        if default_timeout is None:
            default_timeout = _STREAMS_DEFAULT_TIMEOUT_S
        timeout = _validate_timeout(
            args.timeout if args.timeout is not None else default_timeout
        )
        AccessControlGuard(
            cfg,
            "STREAMS",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        ).check(resource, operation)

        if spec["method"] == "publish_binary_record":
            # Bounded file read after the access-control decision and before
            # any client construction (DESIGN-016 binary handling contract).
            file_bytes = _read_file_bounded(
                getattr(args, "file", "") or "", field="file"
            )
            setattr(args, "_file_bytes", file_bytes)

        factory = AsyncClientFactory()
        with factory.invocation_scope(cfg, include_attribution=False):
            root_client = factory.create(cfg, include_attribution=False)
            client = _get_client(root_client, spec["client_path"])
            retry_handler = RetryHandler(timeout_s=timeout)
            result = await retry_handler.execute(_invoke, spec, client, args, timeout)

        formatter = OutputFormatter(format_setting=args.format, pretty=args.pretty)
        print(formatter.format(_model_to_dict(result)))
        return EXIT_SUCCESS
    except AccessControlError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_ACCESS_CONTROL
    except asyncio.CancelledError:
        return _serialize_error(TimeoutError("Operation cancelled"))
    except Exception as exc:
        return _serialize_error(exc)


def console_main() -> int:
    """Run the async CLI through one event-loop boundary."""
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(console_main())
