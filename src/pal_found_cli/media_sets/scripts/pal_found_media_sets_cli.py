#!/usr/bin/env python3
"""Foundry Media Sets CLI - 19 canonical Media Sets API v2 operations.

Exposes exactly 19 public ``foundry_sdk.v2.media_sets`` operations as CLI
subcommands through the single ``MediaSet`` client path:

- media-set (19): abort, calculate, clear, commit, create, get, get-result,
  get-rid-by-path, get-status, info, metadata, read, read-original,
  reference, register, retrieve, transform, upload, upload-media

Four operations return raw ``bytes`` (``get_result``, ``read``,
``read_original``, ``retrieve``). These are dispatched through
``with_streaming_response`` and persisted via ``BinaryDownloadHandler``,
bounded by the FR-DL size limit with the JSON envelope emitted to stdout.

Usage: pal-found-media-sets <resource> <operation> [options]
Output: JSON/TOON on stdout, NDJSON diagnostics on stderr (ADR-004, ADR-005).
Exit codes per ADR-001 taxonomy.
Access control: AccessControlGuard before client construction and file
effects (SRS 4.2, ADR-007). Retry: transient conditions only per ADR-002.
Tracing: SDK-native B3 via invocation_scope; include_attribution=True
per FR-ATTR-4 (media_sets.media_set.* is explicitly attributed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, NoReturn

from pal_found_cli.common.access_control_guard import (
    AccessControlError,
    AccessControlGuard,
)
from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.common.binary_download_handler import BinaryDownloadHandler
from pal_found_cli.common.config_loader import ConfigLoader, ConfigurationError
from pal_found_cli.common.error_serializer import (
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
from pal_found_cli.common.log_setup import LogSetup
from pal_found_cli.common.output_formatter import OutputFormatter
from pal_found_cli.common.retry import RetryHandler
from pal_found_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]

_METADATA_ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "metadata-allow-list.md"

# The four file-producing streamed download commands (DESIGN-018). Each
# acquires a streaming SDK response and persists bounded content through
# BinaryDownloadHandler before opening the destination.
DOWNLOAD_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("media_set", "get_result"),
        ("media_set", "read"),
        ("media_set", "read_original"),
        ("media_set", "retrieve"),
    }
)

# Binary upload operations that read a local file into the bytes body.
_UPLOAD_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("media_set", "upload"),
        ("media_set", "upload_media"),
    }
)

# Bounded binary upload size cap (aligned with FR-DL limits).
_UPLOAD_MAX_BYTES = 16 * 1024 * 1024


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
        "media_set",
        "abort",
        ("MediaSet",),
        "abort",
        ("media_set_rid", "transaction_id"),
        optional=("preview",),
    ),
    _op(
        "media_set",
        "calculate",
        ("MediaSet",),
        "calculate",
        ("media_set_rid", "media_item_rid"),
        optional=("preview", "read_token"),
    ),
    _op(
        "media_set",
        "clear",
        ("MediaSet",),
        "clear",
        ("media_set_rid",),
        required=("media_item_path",),
        optional=("branch_name", "branch_rid", "preview", "transaction_id", "view_rid"),
    ),
    _op(
        "media_set",
        "commit",
        ("MediaSet",),
        "commit",
        ("media_set_rid", "transaction_id"),
        optional=("preview",),
    ),
    _op(
        "media_set",
        "create",
        ("MediaSet",),
        "create",
        ("media_set_rid",),
        optional=("branch_name", "preview"),
    ),
    _op(
        "media_set",
        "get",
        ("MediaSet",),
        "get",
        ("media_set_rid",),
        optional=("preview",),
    ),
    _op(
        "media_set",
        "get_result",
        ("MediaSet",),
        "get_result",
        ("media_set_rid", "media_item_rid", "transformation_job_id"),
        optional=("output", "preview", "token"),
    ),
    _op(
        "media_set",
        "get_rid_by_path",
        ("MediaSet",),
        "get_rid_by_path",
        ("media_set_rid",),
        required=("media_item_path",),
        optional=("branch_name", "branch_rid", "preview", "view_rid"),
    ),
    _op(
        "media_set",
        "get_status",
        ("MediaSet",),
        "get_status",
        ("media_set_rid", "media_item_rid", "transformation_job_id"),
        optional=("preview", "token"),
    ),
    _op(
        "media_set",
        "info",
        ("MediaSet",),
        "info",
        ("media_set_rid", "media_item_rid"),
        optional=("preview", "read_token"),
    ),
    _op(
        "media_set",
        "metadata",
        ("MediaSet",),
        "metadata",
        ("media_set_rid", "media_item_rid"),
        optional=("preview", "read_token"),
    ),
    _op(
        "media_set",
        "read",
        ("MediaSet",),
        "read",
        ("media_set_rid", "media_item_rid"),
        optional=("output", "preview", "read_token"),
    ),
    _op(
        "media_set",
        "read_original",
        ("MediaSet",),
        "read_original",
        ("media_set_rid", "media_item_rid"),
        optional=("output", "preview", "read_token"),
    ),
    _op(
        "media_set",
        "reference",
        ("MediaSet",),
        "reference",
        ("media_set_rid", "media_item_rid"),
        optional=("preview", "read_token"),
    ),
    _op(
        "media_set",
        "register",
        ("MediaSet",),
        "register",
        ("media_set_rid",),
        required=("physical_item_name",),
        optional=("branch_name", "media_item_path", "preview", "transaction_id", "view_rid"),
    ),
    _op(
        "media_set",
        "retrieve",
        ("MediaSet",),
        "retrieve",
        ("media_set_rid", "media_item_rid"),
        optional=("output", "preview", "read_token"),
    ),
    _op(
        "media_set",
        "transform",
        ("MediaSet",),
        "transform",
        ("media_set_rid", "media_item_rid"),
        required=("transformation",),
        optional=("preview", "token"),
        json_args=frozenset({"transformation"}),
    ),
    _op(
        "media_set",
        "upload",
        ("MediaSet",),
        "upload",
        ("media_set_rid",),
        required=("file",),
        optional=("branch_name", "branch_rid", "media_item_path", "media_item_rid", "preview", "transaction_id", "view_rid"),
    ),
    _op(
        "media_set",
        "upload_media",
        ("MediaSet",),
        "upload_media",
        required=("file", "filename"),
        optional=("media_item_rid", "preview"),
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

# Boolean-typed SDK options registered as store_true flags.
_BOOLEAN_ARG_NAMES: frozenset[str] = frozenset({"preview"})


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
    parser.add_argument(flag, required=required, default=None, dest=arg_name)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 19 Media Sets operations."""
    parser = _ArgumentParser(
        prog="pal-found-media-sets",
        description="Foundry Media Sets CLI - 19 Media Sets API v2 operations",
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

    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return one catalog operation specification."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise CLIInputError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(root_client: Any, client_path: tuple[str, ...]) -> Any:
    """Resolve the exact public nested Media Sets SDK resource."""
    client = root_client.media_sets
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


def _validate_timeout(value: int) -> int:
    """Validate the effective per-attempt timeout from ADR-002."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise CLIInputError("timeout must be between 1 and 3600 seconds")
    return value


def _read_file_bounded(path_value: str, *, field: str) -> bytes:
    """Read a local file for binary upload with a hard size bound."""
    path = Path(path_value)
    if not path.is_file():
        raise CLIInputError(f"{field} must reference an existing file")
    size = path.stat().st_size
    if size > _UPLOAD_MAX_BYTES:
        raise CLIInputError(f"{field} exceeds the maximum upload size")
    return path.read_bytes()


def _parse_json_list(value: str, *, field: str) -> list[Any]:
    """Decode one JSON array."""
    result = _decode_json(value, field=field)
    if not isinstance(result, list):
        raise CLIInputError(f"{field} must be a JSON array")
    return result


def _validate_inputs(spec: OperationSpec, args: argparse.Namespace) -> None:
    """Validate scalars and decode JSON arguments before client creation."""
    for name in (*spec["positional"], *spec["required"]):
        if name == "file":
            continue
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

    ``--file`` on upload/upload_media is consumed separately and never
    forwarded. ``--output`` on download operations is consumed by the
    BinaryDownloadHandler path and never forwarded.
    """
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        if name in {"file", "output"}:
            continue
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = value
    kwargs["request_timeout"] = timeout
    return kwargs


async def _invoke(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
) -> Any:
    """Invoke one non-downloaded SDK operation."""
    positional = [getattr(args, name) for name in spec["positional"]]
    if (spec["resource"], spec["operation"]) in _UPLOAD_OPS:
        positional.append(getattr(args, "_file_bytes"))
    return await getattr(client, spec["method"])(
        *positional, **_build_kwargs(spec, args, timeout)
    )


async def _download_operation(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
    cfg: ConfigLoader,
) -> dict[str, Any]:
    """Stream one bounded media download through BinaryDownloadHandler."""
    method = getattr(getattr(client, "with_streaming_response"), spec["method"])
    positional = [getattr(args, name) for name in spec["positional"]]
    kwargs: dict[str, Any] = {}
    for name in spec["optional"]:
        if name == "output":
            continue
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = value
    response_context = method(*positional, request_timeout=timeout, **kwargs)
    async with response_context as response:
        result = await BinaryDownloadHandler(config=cfg).save(
            response.aiter_bytes(),
            original_filename=getattr(args, "output", None),
            namespace="media_sets",
            operation=f"{spec['resource']}.{spec['method']}",
            content_length=None,
            content_encoding=None,
            mime_type=None,
        )
    return result.to_dict()


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
    message = str(exception) if safe else "Media Sets operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if isinstance(status, int):
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "Media Sets operation failed",
        extra={
            "call_id": serializer.call_id,
            "exception_type": type(exception).__name__,
            "http_status": status,
        },
    )
    return exit_code


async def main() -> int:
    """Run one stateless Foundry Media Sets invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("a Media Sets operation is required")
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
        timeout = _validate_timeout(
            args.timeout if args.timeout is not None else cfg.timeout_s
        )
        AccessControlGuard(
            cfg,
            "MEDIA_SETS",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        ).check(resource, operation)

        if (resource, operation) in _UPLOAD_OPS:
            # Bounded file read after the access-control decision and before
            # any client construction (DESIGN-018 binary handling contract).
            file_bytes = _read_file_bounded(
                getattr(args, "file", "") or "", field="file"
            )
            setattr(args, "_file_bytes", file_bytes)

        factory = AsyncClientFactory()
        with factory.invocation_scope(cfg, include_attribution=True):
            root_client = factory.create(cfg, include_attribution=True)
            client = _get_client(root_client, spec["client_path"])
            retry_handler = RetryHandler(timeout_s=timeout)
            if (resource, operation) in DOWNLOAD_OPS:
                result = await retry_handler.execute(
                    _download_operation, spec, client, args, timeout, cfg
                )
            else:
                result = await retry_handler.execute(
                    _invoke, spec, client, args, timeout
                )

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
