#!/usr/bin/env python3
"""Foundry Audit CLI for log-file listing and bounded content downloads."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import date
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
from foundry_cli.common.binary_download_handler import (  # noqa: E402
    BinaryDownloadHandler,
)
from foundry_cli.common.config_loader import ConfigLoader  # noqa: E402
from foundry_cli.common.error_serializer import (  # noqa: E402
    EXIT_ACCESS_CONTROL,
    EXIT_SERVER_ERROR,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    EXIT_USER_INPUT,
    ErrorSerializer,
)
from foundry_cli.common.log_setup import LogSetup  # noqa: E402
from foundry_cli.common.output_formatter import OutputFormatter  # noqa: E402
from foundry_cli.common.pagination_helper import PaginationHelper  # noqa: E402
from foundry_cli.common.retry import RetryHandler  # noqa: E402

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _op(
    operation: str,
    method: str,
    positional: Iterable[str],
    optional: Iterable[str],
) -> OperationSpec:
    """Build one immutable-shape operation catalog entry."""
    return {
        "resource": "log_file",
        "operation": operation,
        "client_path": "Organization.LogFile",
        "method": method,
        "positional": tuple(positional),
        "optional": tuple(optional),
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op(
        "list",
        "list",
        ("organization_rid",),
        (
            "start_date",
            "end_date",
            "page_size",
            "page_token",
            "batch_pages",
        ),
    ),
    _op(
        "content",
        "content",
        ("organization_rid", "log_file_id"),
        ("output_filename",),
    ),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {
    "log_file": {spec["operation"]: spec for spec in OP_SPECS}
}


def _common_parser(*, paginated: bool) -> argparse.ArgumentParser:
    """Build operation-level common options."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--format", choices=("json", "toon", "auto"), default="auto")
    parser.add_argument("--pretty", action="store_true")
    if paginated:
        parser.add_argument("--page-size", type=int, default=None, dest="page_size")
        parser.add_argument("--page-token", default=None, dest="page_token")
        parser.add_argument("--batch-pages", type=int, default=None, dest="batch_pages")
    return parser


def build_parser() -> argparse.ArgumentParser:
    """Build the parser for the two Foundry Audit operations."""
    parser = argparse.ArgumentParser(
        prog="foundry_audit_cli",
        description="Foundry Audit CLI - 2 log-file operations",
        epilog="Operations: log-file list; log-file content",
    )
    resource_parsers = parser.add_subparsers(dest="resource", help="Resource type")
    log_file_parser = resource_parsers.add_parser("log-file")
    operation_parsers = log_file_parser.add_subparsers(dest="operation")

    list_parser = operation_parsers.add_parser(
        "list", parents=[_common_parser(paginated=True)]
    )
    list_parser.add_argument("organization_rid")
    list_parser.add_argument("--start-date", default=None, dest="start_date")
    list_parser.add_argument("--end-date", default=None, dest="end_date")

    content_parser = operation_parsers.add_parser(
        "content", parents=[_common_parser(paginated=False)]
    )
    content_parser.add_argument("organization_rid")
    content_parser.add_argument("log_file_id")
    content_parser.add_argument(
        "--output-filename", default=None, dest="output_filename"
    )
    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return the catalog entry for an Audit operation."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {resource}.{operation}") from exc


def _parse_iso_date(value: str | None, *, field: str) -> date | None:
    """Parse one strict ``YYYY-MM-DD`` calendar date."""
    if value is None:
        return None
    if _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid calendar date") from exc


def _validate_list_cursor(start_date: date | None, page_token: str | None) -> None:
    """Require a start date for an initial list request."""
    if start_date is None and not page_token:
        raise ValueError("start_date is required when page_token is not provided")


def _get_client(
    cfg: ConfigLoader,
    resource: str,
    factory: AsyncClientFactory | None = None,
) -> Any:
    """Create and resolve the nested SDK client for an Audit resource."""
    client = (factory or AsyncClientFactory()).create(cfg).audit
    spec = next(iter(OPERATION_BY_RESOURCE[resource].values()))
    for attr in spec["client_path"].split("."):
        client = getattr(client, attr)
    return client


def _model_to_dict(value: Any) -> Any:
    """Convert SDK and Pydantic models into JSON-compatible values."""
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


async def _fetch_list_page(
    client: Any,
    *,
    organization_rid: str,
    start_date: date | None,
    end_date: date | None,
    page_size: int,
    page_token: str | None,
    request_timeout: int | None,
) -> dict[str, Any]:
    """Fetch and adapt one raw SDK Audit page."""
    raw_response = await client.with_raw_response.list(
        organization_rid,
        start_date=start_date,
        end_date=end_date,
        page_size=page_size,
        page_token=page_token,
        request_timeout=request_timeout,
    )
    response = raw_response.decode()
    return {
        "items": list(getattr(response, "data", None) or []),
        "next_page_token": getattr(response, "next_page_token", None),
    }


async def _list_log_files(
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
) -> tuple[list[Any], PaginationHelper]:
    """Collect one bounded batch of actual server pages."""
    helper = PaginationHelper(
        page_size=args.page_size,
        page_token=args.page_token,
        batch_pages=args.batch_pages,
    )

    async def fetch_page(**page_kwargs: Any) -> dict[str, Any]:
        return await _fetch_list_page(
            client,
            organization_rid=args.organization_rid,
            start_date=args.start_date,
            end_date=args.end_date,
            page_size=page_kwargs["page_size"],
            page_token=page_kwargs.get("page_token"),
            request_timeout=timeout,
        )

    records = await helper.paginate(fetch_page)
    return records, helper


async def _download_content(
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
    cfg: ConfigLoader,
) -> dict[str, Any]:
    """Stream one log file through the bounded atomic download handler."""
    response_context = client.with_streaming_response.content(
        args.organization_rid,
        args.log_file_id,
        request_timeout=timeout,
    )
    async with response_context as response:
        result = await BinaryDownloadHandler(config=cfg).save(
            response.aiter_bytes(),
            original_filename=args.output_filename,
            namespace="audit",
            operation="log_file.content",
            content_length=None,
            content_encoding=None,
            mime_type=None,
        )
    return result.to_dict()


def _serialize_error(exception: BaseException) -> int:
    """Serialize an exception and retain the server-error fallback contract."""
    serializer = ErrorSerializer()
    exit_code = serializer.serialize(exception, print_to_stdout=False)
    if exit_code == EXIT_USER_INPUT and not isinstance(exception, (TypeError, ValueError)):
        exit_code = EXIT_SERVER_ERROR
    envelope = serializer.create_error_envelope(
        exit_code,
        str(exception),
        type(exception).__name__,
        serializer.call_id,
    )
    response = getattr(exception, "response", None)
    http_status = getattr(response, "status_code", None)
    if isinstance(http_status, int):
        envelope["http_status"] = http_status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    return exit_code


async def main() -> int:
    """Run the Foundry Audit CLI."""
    parser = build_parser()
    args = parser.parse_args()
    if not args.resource or not getattr(args, "operation", None):
        parser.print_help()
        return EXIT_USER_INPUT

    try:
        cfg = ConfigLoader()
        cfg.load()
        LogSetup.configure(log_level=cfg.log_level)

        resource = args.resource.replace("-", "_")
        operation = args.operation.replace("-", "_")
        _spec_for(resource, operation)

        if operation == "list":
            args.start_date = _parse_iso_date(args.start_date, field="start_date")
            args.end_date = _parse_iso_date(args.end_date, field="end_date")
            _validate_list_cursor(args.start_date, args.page_token)

        AccessControlGuard(cfg, "AUDIT").check(resource, operation)

        factory = AsyncClientFactory()
        helper: PaginationHelper | None = None
        with factory.invocation_scope(cfg):
            client = _get_client(cfg, resource, factory)
            timeout = args.timeout or cfg.timeout_s
            retry_handler = RetryHandler()
            if operation == "list":
                result, helper = await retry_handler.execute(
                    _list_log_files, client, args, timeout
                )
            else:
                result = await retry_handler.execute(
                    _download_content, client, args, timeout, cfg
                )

        formatter = OutputFormatter(
            format_setting="json" if operation == "content" else args.format,
            pretty=args.pretty,
        )
        print(formatter.format(_model_to_dict(result)))
        if helper is not None:
            helper.emit_metadata()
        return EXIT_SUCCESS
    except AccessControlError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_ACCESS_CONTROL
    except asyncio.CancelledError:
        cancellation = TimeoutError("Operation cancelled")
        ErrorSerializer().serialize(cancellation)
        return EXIT_TIMEOUT
    except Exception as exc:
        return _serialize_error(exc)


def console_main() -> int:
    """Run the async CLI through one event-loop boundary."""
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(console_main())
