#!/usr/bin/env python3
"""Foundry Widgets CLI - 8 canonical Widgets API v2 operations.

Exposes exactly 8 public ``foundry_sdk.v2.widgets`` operations as CLI
subcommands across the DevModeSettings, Repository, WidgetSet, and nested
WidgetSet.Release client paths:

- dev-mode-settings (2): enable, set-widget-set-by-id
- release (3): delete, get, list
- repository (2): get, publish
- widget-set (1): get

The catalog follows the corrected operation surface verified against the
installed runtime SDK (foundry-platform-sdk 1.102.0, QUESTION-043
decision): the four DevModeSettings operations ``disable``/``get``/
``pause``/``set_widget_set`` and the ``DevModeSettingsV2`` resource are
NOT present on the installed SDK and are intentionally not implemented.

``release list`` returns an SDK ``ResourceIterator`` and implements the
ADR-003 cursor-paged pattern via PaginationHelper with
``--page-size``/``--page-token``/``--all``/``--max-pages``.

``repository publish`` accepts the widget-set build as a zip file that must
include a valid manifest at ``.palantir/widgets.config.json``. The CLI
reads ``--file`` with a bounded read (16 MiB) after the access-control
decision and before client construction. The ``--file`` flag is consumed
by the CLI and never forwarded; ``--repository-version`` is passed as the
SDK ``repository_version`` query parameter.

Usage: foundry-widgets <resource> <operation> [options]
Output: JSON/TOON on stdout, NDJSON diagnostics on stderr (ADR-004, ADR-005).
Exit codes per ADR-001 taxonomy.
Access control: AccessControlGuard before client construction and file
effects (SRS 4.2, ADR-007). Retry: transient conditions only per ADR-002.
Tracing: SDK-native B3 via invocation_scope; include_attribution=False
(DESIGN-022; this namespace is outside FR-ATTR-4 scope).
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
from foundry_cli.common.pagination_helper import HARD_MAX_BATCH_PAGES, PaginationHelper
from foundry_cli.common.retry import RetryHandler
from foundry_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]

_METADATA_ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "metadata-allow-list.md"

# The single cursor-paged command (DESIGN-022 paging contract). The SDK list
# method returns core.ResourceIterator and accepts page_size/page_token.
PAGINATED_OPS: frozenset[tuple[str, str]] = frozenset({("release", "list")})

# Bounded binary upload size cap for widget-set build zips (aligned with the
# standard upload bound used across namespaces, DESIGN-022 binary contract).
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
        "dev_mode_settings",
        "enable",
        ("DevModeSettings",),
        "enable",
    ),
    _op(
        "dev_mode_settings",
        "set_widget_set_by_id",
        ("DevModeSettings",),
        "set_widget_set_by_id",
        required=("widget_set_rid", "settings"),
        json_args=frozenset({"settings"}),
    ),
    _op(
        "release",
        "delete",
        ("WidgetSet", "Release"),
        "delete",
        ("widget_set_rid", "release_version"),
    ),
    _op(
        "release",
        "get",
        ("WidgetSet", "Release"),
        "get",
        ("widget_set_rid", "release_version"),
    ),
    _op(
        "release",
        "list",
        ("WidgetSet", "Release"),
        "list",
        ("widget_set_rid",),
    ),
    _op(
        "repository",
        "get",
        ("Repository",),
        "get",
        ("repository_rid",),
    ),
    _op(
        "repository",
        "publish",
        ("Repository",),
        "publish",
        ("repository_rid",),
        required=("repository_version", "file"),
    ),
    _op(
        "widget_set",
        "get",
        ("WidgetSet",),
        "get",
        ("widget_set_rid",),
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
    else:
        flag = "--" + _kebab(arg_name)
    kwargs: dict[str, Any] = {"required": required, "default": None, "dest": arg_name}
    parser.add_argument(flag, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 8 Widgets operations."""
    parser = _ArgumentParser(
        prog="foundry-widgets",
        description="Foundry Widgets CLI - 8 Widgets API v2 operations",
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
    """Resolve the exact public nested Widgets SDK resource."""
    client = root_client.widgets
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
    """Read a local zip file for a bounded binary upload."""
    path = Path(path_value)
    if not path.is_file():
        raise CLIInputError(f"{field} must reference an existing file")
    size = path.stat().st_size
    if size > _UPLOAD_MAX_BYTES:
        raise CLIInputError(f"{field} exceeds the maximum upload size")
    return path.read_bytes()


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

    For cursor-paged operations the page_size/page_token come from the
    PaginationHelper path and are intentionally omitted here. ``--file``
    on publish is consumed separately and never forwarded.
    """
    is_paginated = (spec["resource"], spec["operation"]) in PAGINATED_OPS
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        if name == "file":
            continue
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
    if spec["method"] == "publish":
        positional.append(getattr(args, "_file_bytes"))
    return await getattr(client, spec["method"])(
        *positional, **_build_kwargs(spec, args, timeout)
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
    message = str(exception) if safe else "Widgets operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if isinstance(status, int):
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "Widgets operation failed",
        extra={
            "call_id": serializer.call_id,
            "exception_type": type(exception).__name__,
            "http_status": status,
        },
    )
    return exit_code


async def main() -> int:
    """Run one stateless Foundry Widgets invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("a Widgets operation is required")
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
            "WIDGETS",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        ).check(resource, operation)

        if spec["method"] == "publish":
            # Bounded file read after the access-control decision and before
            # any client construction (DESIGN-022 binary handling contract).
            file_bytes = _read_file_bounded(
                getattr(args, "file", "") or "", field="file"
            )
            setattr(args, "_file_bytes", file_bytes)

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
