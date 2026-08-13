#!/usr/bin/env python3
"""Foundry Orchestration CLI - 20 canonical Orchestration API v2 operations.

Exposes exactly 20 public ``foundry_sdk.v2.orchestration`` operations as CLI
subcommands across Build, Job, Schedule, and ScheduleVersion client paths:

- build (6): cancel, create, get, get-batch, jobs, search
- job (2): get, get-batch
- schedule (10): create, delete, get, get-affected-resources, get-batch,
  pause, replace, run, runs, unpause
- schedule-version (2): get, schedule

The ScheduleRun sub-client exists in the SDK but exposes no public methods;
it must not appear in ``OP_SPECS``.

Usage: pal-found-orchestration <resource> <operation> [options]
Output: JSON/TOON on stdout, NDJSON diagnostics on stderr (ADR-004, ADR-005).
Exit codes per ADR-001 taxonomy.
Access control: AccessControlGuard before client construction (SRS 4.2,
ADR-007). Retry: transient conditions only per ADR-002.
Tracing: SDK-native B3 via invocation_scope; include_attribution=False.
No binary downloads: BinaryDownloadHandler is not required.
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
from pal_found_cli.common.pagination_helper import HARD_MAX_BATCH_PAGES, PaginationHelper
from pal_found_cli.common.retry import RetryHandler
from pal_found_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]

_METADATA_ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[1] / "metadata-allow-list.md"
)

# The three cursor-paged commands (DESIGN-014 paging contract). Batch
# get_batch commands and build search are single-call responses.
PAGINATED_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("build", "jobs"),
        ("build", "search"),
        ("schedule", "runs"),
    }
)

# The eight mutating operations (DESIGN-014 write set).
WRITE_OPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("build", "cancel"),
        ("build", "create"),
        ("schedule", "create"),
        ("schedule", "delete"),
        ("schedule", "pause"),
        ("schedule", "replace"),
        ("schedule", "run"),
        ("schedule", "unpause"),
    }
)


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
    _op("build", "cancel", ("Build",), "cancel", ("build_rid",)),
    _op(
        "build",
        "create",
        ("Build",),
        "create",
        required=("target", "fallback_branches"),
        optional=(
            "abort_on_failure",
            "branch_name",
            "force_build",
            "notifications_enabled",
            "retry_backoff_duration",
            "retry_count",
        ),
        json_args=frozenset({"target", "retry_backoff_duration"}),
        list_json_args=frozenset({"fallback_branches"}),
    ),
    _op("build", "get", ("Build",), "get", ("build_rid",)),
    _op(
        "build",
        "get_batch",
        ("Build",),
        "get_batch",
        required=("build_rids",),
        list_json_args=frozenset({"build_rids"}),
    ),
    _op(
        "build",
        "jobs",
        ("Build",),
        "jobs",
        ("build_rid",),
        optional=("page_size", "page_token"),
    ),
    _op(
        "build",
        "search",
        ("Build",),
        "search",
        optional=("where", "order_by", "page_size", "page_token"),
        json_args=frozenset({"where", "order_by"}),
    ),
    _op("job", "get", ("Job",), "get", ("job_rid",)),
    _op(
        "job",
        "get_batch",
        ("Job",),
        "get_batch",
        required=("job_rids",),
        list_json_args=frozenset({"job_rids"}),
    ),
    _op(
        "schedule",
        "create",
        ("Schedule",),
        "create",
        required=("action", "trigger", "scope_mode"),
        optional=("display_name", "description"),
        json_args=frozenset({"action", "trigger", "scope_mode"}),
    ),
    _op("schedule", "delete", ("Schedule",), "delete", ("schedule_rid",)),
    _op("schedule", "get", ("Schedule",), "get", ("schedule_rid",)),
    _op(
        "schedule",
        "get_affected_resources",
        ("Schedule",),
        "get_affected_resources",
        ("schedule_rid",),
    ),
    _op(
        "schedule",
        "get_batch",
        ("Schedule",),
        "get_batch",
        required=("schedule_rids",),
        list_json_args=frozenset({"schedule_rids"}),
    ),
    _op("schedule", "pause", ("Schedule",), "pause", ("schedule_rid",)),
    _op(
        "schedule",
        "replace",
        ("Schedule",),
        "replace",
        ("schedule_rid",),
        required=("action", "trigger", "scope_mode"),
        optional=("display_name", "description"),
        json_args=frozenset({"action", "trigger", "scope_mode"}),
    ),
    _op("schedule", "run", ("Schedule",), "run", ("schedule_rid",)),
    _op(
        "schedule",
        "runs",
        ("Schedule",),
        "runs",
        ("schedule_rid",),
        optional=("page_size", "page_token"),
    ),
    _op("schedule", "unpause", ("Schedule",), "unpause", ("schedule_rid",)),
    _op(
        "schedule_version",
        "get",
        ("ScheduleVersion",),
        "get",
        ("schedule_version_rid",),
    ),
    _op(
        "schedule_version",
        "schedule",
        ("ScheduleVersion",),
        "schedule",
        ("schedule_version_rid",),
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

# Boolean-typed SDK options are registered as store_true flags.
_BOOLEAN_ARG_NAMES: frozenset[str] = frozenset(
    {"force_build", "abort_on_failure", "notifications_enabled"}
)

# Integer-typed SDK options.
_INT_ARG_NAMES: frozenset[str] = frozenset({"retry_count"})


def _common_parser(*, paginated: bool) -> _ArgumentParser:
    """Build operation-level common options."""
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--format", choices=("json", "toon", "auto"), default="auto")
    parser.add_argument("--pretty", action="store_true")
    if paginated:
        parser.add_argument("--page-size", type=int, default=None, dest="page_size")
        parser.add_argument("--page-token", default=None, dest="page_token")
        parser.add_argument("--all", action="store_true", dest="all_pages")
        parser.add_argument("--max-pages", type=int, default=None, dest="max_pages")
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
    """Build argparse parser with all 20 Orchestration operations."""
    parser = _ArgumentParser(
        prog="pal-found-orchestration",
        description="Foundry Orchestration CLI - 20 Orchestration API v2 operations",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    for resource in sorted(OPERATION_BY_RESOURCE):
        res_parser = subparsers.add_parser(_kebab(resource))
        op_sub = res_parser.add_subparsers(dest="operation")
        for operation, spec in sorted(OPERATION_BY_RESOURCE[resource].items()):
            is_paginated = (resource, operation) in PAGINATED_OPS
            op_parser = op_sub.add_parser(
                _kebab(operation), parents=[_common_parser(paginated=is_paginated)]
            )
            for arg_name in spec["positional"]:
                op_parser.add_argument(arg_name)
            for arg_name in spec["required"]:
                _add_kwarg(op_parser, arg_name, required=True)
            for arg_name in spec["optional"]:
                if arg_name in {"page_size", "page_token"} and is_paginated:
                    continue
                _add_kwarg(op_parser, arg_name, required=False)

    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return one catalog operation specification."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise CLIInputError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(root_client: Any, client_path: tuple[str, ...]) -> Any:
    """Resolve the exact public nested Orchestration SDK resource."""
    client = root_client.orchestration
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


def _validate_inputs(spec: OperationSpec, args: argparse.Namespace) -> None:
    """Validate scalars and decode JSON arguments before client creation."""
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
    """Build SDK keyword arguments, omitting absent optional values."""
    is_paginated = (spec["resource"], spec["operation"]) in PAGINATED_OPS
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        if name in {"page_size", "page_token"} and is_paginated:
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
    """Invoke one non-paginated SDK operation."""
    positional = [getattr(args, name) for name in spec["positional"]]
    kwargs = _build_kwargs(spec, args, timeout)
    if spec["operation"] == "get_batch":
        # The CLI accepts a flat list of RIDs; the SDK body expects one
        # request element per RID.
        rid_field = {
            "build": "build_rid",
            "job": "job_rid",
            "schedule": "schedule_rid",
        }[spec["resource"]]
        rids_key = f"{spec['resource']}_rids"
        rids = kwargs.pop(rids_key)
        kwargs["body"] = [{rid_field: rid} for rid in rids]
    return await getattr(client, spec["method"])(
        *positional, **kwargs
    )


async def _fetch_raw_page(
    method: Any,
    *,
    page_size: int,
    page_token: str | None,
    request_timeout: int | None,
    positional: tuple[Any, ...],
    extra_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Fetch exactly one decoded SDK page via with_raw_response."""
    raw_response = await method(
        *positional,
        page_size=page_size,
        page_token=page_token,
        request_timeout=request_timeout,
        **extra_kwargs,
    )
    page = raw_response.decode()
    return {
        "items": list(getattr(page, "data", None) or []),
        "next_page_token": getattr(page, "next_page_token", None),
    }


def _resolve_pagination_flags(args: argparse.Namespace) -> int:
    """Resolve the effective page batch size from --all/--max-pages."""
    if getattr(args, "all_pages", False):
        return HARD_MAX_BATCH_PAGES
    return getattr(args, "max_pages", None) or 1


async def _paginate_operation(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
) -> tuple[list[Any], PaginationHelper]:
    """Collect a bounded number of actual server pages."""
    raw_method = getattr(getattr(client, "with_raw_response"), spec["method"])
    batch_pages = _resolve_pagination_flags(args)
    helper = PaginationHelper(
        page_size=getattr(args, "page_size", None),
        page_token=getattr(args, "page_token", None),
        batch_pages=batch_pages,
    )
    positional = tuple(getattr(args, name) for name in spec["positional"])
    extra_kwargs: dict[str, Any] = {}
    for name in spec["optional"]:
        if name in {"page_size", "page_token"}:
            continue
        value = getattr(args, name, None)
        if value is not None:
            extra_kwargs[name] = value

    async def fetch_page(**page_kwargs: Any) -> dict[str, Any]:
        return await _fetch_raw_page(
            raw_method,
            page_size=page_kwargs["page_size"],
            page_token=page_kwargs.get("page_token"),
            request_timeout=timeout,
            positional=positional,
            extra_kwargs=extra_kwargs,
        )

    return await helper.paginate(fetch_page), helper


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
    message = str(exception) if safe else "Orchestration operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if isinstance(status, int):
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "Orchestration operation failed",
        extra={
            "call_id": serializer.call_id,
            "exception_type": type(exception).__name__,
            "http_status": status,
        },
    )
    return exit_code


async def main() -> int:
    """Run one stateless Foundry Orchestration invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("an Orchestration operation is required")
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
            "ORCHESTRATION",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        ).check(resource, operation)

        factory = AsyncClientFactory()
        helper: PaginationHelper | None = None
        with factory.invocation_scope(cfg, include_attribution=False):
            root_client = factory.create(cfg, include_attribution=False)
            client = _get_client(root_client, spec["client_path"])
            retry_handler = RetryHandler(timeout_s=timeout)
            if (resource, operation) in PAGINATED_OPS:
                result, helper = await retry_handler.execute(
                    _paginate_operation, spec, client, args, timeout
                )
            else:
                result = await retry_handler.execute(
                    _invoke, spec, client, args, timeout
                )

        formatter = OutputFormatter(format_setting=args.format, pretty=args.pretty)
        print(formatter.format(_model_to_dict(result)))
        if helper is not None:
            helper.emit_metadata()
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
