#!/usr/bin/env python3
"""Foundry Datasets CLI - 33 operations across 5 resource clients.

Exposes all Foundry Datasets API v2 operations as CLI subcommands:
- Dataset (11): create, get, get-health-check-reports, get-health-checks,
  get-schedules, get-schema, get-schema-batch, jobs, put-schema, read-table, transactions
- Branch (5): create, delete, get, list, transactions
- File (5): content, delete, get, list, upload
- Transaction (6): abort, build, commit, create, get, job
- View (6): add-backing-datasets, add-primary-key, create, get,
  remove-backing-datasets, replace-backing-datasets

Usage: python pal_found_datasets_cli.py <resource> <operation> [options]
Output: JSON/TOON on stdout, metadata on stderr (ADR-004, ADR-005).
Exit codes per ADR-001 taxonomy.
Access Control: Auth -> Access Control -> API call order (SRS 4.2, ADR-007).
Retry: Exponential backoff with jitter per ADR-002.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, cast

from pal_found_cli.common.cli_parser import ArgumentParser as _ArgumentParser  # noqa: E402
from pal_found_cli.common.cli_parser import CLIInputError  # noqa: E402
from pal_found_cli.common.access_control_guard import (
    AccessControlError,
    AccessControlGuard,
)
from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.common.binary_download_handler import (
    BinaryDownloadHandler,
    DownloadError,
)
from pal_found_cli.common.config_loader import ConfigLoader
from pal_found_cli.common.error_serializer import (
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
from pal_found_cli.common.log_setup import LogSetup
from pal_found_cli.common.output_formatter import OutputFormatter
from pal_found_cli.common.pagination_helper import PaginationHelper
from pal_found_cli.common.retry import RetryHandler
from pal_found_cli.common.timeout_config import resolve_timeout

logger = logging.getLogger(__name__)


def _model_to_dict(obj: Any) -> Any:
    """Convert Pydantic model to serializable dict.

    Handles Pydantic v2 (model_dump), Pydantic v1 (dict), nested collections.
    Checks list/dict before model methods to avoid treating collections as models.
    Uses try/except so MagicMock or objects with dynamic attributes don't corrupt output.
    """
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_model_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _model_to_dict(v) for k, v in obj.items()}
    # Try Pydantic v2 first
    try:
        result = obj.model_dump()
        if isinstance(result, dict):
            return result
    except (AttributeError, TypeError):
        pass
    # Fallback to Pydantic v1
    try:
        result = obj.dict()
        if isinstance(result, dict):
            return result
    except (AttributeError, TypeError):
        pass
    return obj


def _get_client(cfg: ConfigLoader, resource: str) -> Any:
    """Get the SDK client for a resource.

    Parameters
    ----------
    cfg : ConfigLoader
        Configuration instance.
    resource : str
        Resource name: dataset, branch, file, transaction, view.

    Returns
    -------
    Any
        SDK client for the requested resource.
    """
    client = AsyncClientFactory().create(cfg).datasets.Dataset
    resource_map = {
        "branch": "Branch",
        "file": "File",
        "transaction": "Transaction",
        "view": "View",
    }
    attr = resource_map.get(resource)
    return getattr(client, attr) if attr else client


async def _invoke(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
) -> Any:
    """Invoke SDK operation (async).

    Parameters
    ----------
    resource : str
        Resource name.
    operation : str
        snake_case operation name.
    client : Any
        SDK client instance.
    args : argparse.Namespace
        Parsed CLI arguments.
    timeout : Optional[int]
        Request timeout in seconds. May be ``None`` to let the SDK decide.

    Returns
    -------
    Any
        SDK response object.

    Raises
    ------
    ValueError
        If the operation is not recognized.
    """
    dr = getattr(args, "dataset_rid", None)
    bn = getattr(args, "branch_name", None)
    tr = getattr(args, "transaction_rid", None)
    ps = getattr(args, "page_size", None)
    pt = getattr(args, "page_token", None)
    fp = getattr(args, "file_path", None)
    vr = getattr(args, "view_dataset_rid", None)
    br = getattr(args, "branch", None)
    nm = getattr(args, "name", None)
    pfr = getattr(args, "parent_folder_rid", None)

    if resource == "dataset":
        if operation == "create":
            return await client.create(
                name=nm, parent_folder_rid=pfr, request_timeout=timeout
            )
        if operation == "get":
            return await client.get(dataset_rid=dr, request_timeout=timeout)
        if operation == "get_health_check_reports":
            return await client.get_health_check_reports(
                dataset_rid=dr, branch_name=bn, request_timeout=timeout
            )
        if operation == "get_health_checks":
            return await client.get_health_checks(
                dataset_rid=dr, branch_name=bn, request_timeout=timeout
            )
        if operation == "get_schedules":
            return await client.get_schedules(
                dataset_rid=dr,
                branch_name=bn,
                page_size=ps,
                page_token=pt,
                request_timeout=timeout,
            )
        if operation == "get_schema":
            return await client.get_schema(
                dataset_rid=dr, branch_name=bn, request_timeout=timeout
            )
        if operation == "get_schema_batch":
            body = json.loads(args.body_json)
            if (
                not isinstance(body, list)
                or not 1 <= len(body) <= 1000
                or not all(
                    isinstance(item, dict) and isinstance(item.get("datasetRid"), str)
                    for item in body
                )
            ):
                raise ValueError(
                    "--body-json must contain 1-1000 objects with datasetRid"
                )
            return await client.get_schema_batch(body, request_timeout=timeout)
        if operation == "jobs":
            order_by = json.loads(args.order_by_json)
            if not isinstance(order_by, list) or not all(
                isinstance(item, dict) for item in order_by
            ):
                raise ValueError("--order-by-json must be a JSON array of sort objects")
            return await client.jobs(
                dataset_rid=dr,
                order_by=order_by,
                branch_name=bn,
                page_size=ps,
                page_token=pt,
                request_timeout=timeout,
            )
        if operation == "put_schema":
            schema_arg = getattr(args, "schema", None)
            schema = (
                json.loads(schema_arg) if isinstance(schema_arg, str) else schema_arg
            )
            return await client.put_schema(
                dataset_rid=dr, schema=schema, branch_name=bn, request_timeout=timeout
            )
        if operation == "transactions":
            return await client.transactions(
                dataset_rid=dr, page_size=ps, page_token=pt, request_timeout=timeout
            )
    elif resource == "branch":
        if operation == "create":
            return await client.create(
                dataset_rid=dr, name=nm, transaction_rid=tr, request_timeout=timeout
            )
        if operation == "delete":
            return await client.delete(
                dataset_rid=dr, branch_name=bn, request_timeout=timeout
            )
        if operation == "get":
            return await client.get(
                dataset_rid=dr, branch_name=bn, request_timeout=timeout
            )
        if operation == "list":
            return await client.list(
                dataset_rid=dr, page_size=ps, page_token=pt, request_timeout=timeout
            )
        if operation == "transactions":
            return await client.transactions(
                dataset_rid=dr,
                branch_name=bn,
                page_size=ps,
                page_token=pt,
                request_timeout=timeout,
            )
    elif resource == "file":
        if operation == "delete":
            return await client.delete(
                dataset_rid=dr,
                file_path=fp,
                transaction_rid=tr,
                request_timeout=timeout,
            )
        if operation == "get":
            return await client.get(
                dataset_rid=dr,
                file_path=fp,
                branch_name=bn,
                end_transaction_rid=getattr(args, "end_transaction_rid", None),
                start_transaction_rid=getattr(args, "start_transaction_rid", None),
                request_timeout=timeout,
            )
        if operation == "list":
            return await client.list(
                dataset_rid=dr,
                branch_name=bn,
                end_transaction_rid=getattr(args, "end_transaction_rid", None),
                page_size=ps,
                page_token=pt,
                path_prefix=getattr(args, "path_prefix", None),
                start_transaction_rid=getattr(args, "start_transaction_rid", None),
                request_timeout=timeout,
            )
        if operation == "upload":
            if fp is None:
                raise ValueError("file_path is required for upload operation")
            with open(fp, "rb") as fobj:
                data = fobj.read()
            return await client.upload(
                dataset_rid=dr,
                file_path=fp,
                body=data,
                transaction_rid=tr,
                request_timeout=timeout,
            )
    elif resource == "transaction":
        if operation == "abort":
            return await client.abort(
                dataset_rid=dr, transaction_rid=tr, request_timeout=timeout
            )
        if operation == "build":
            return await client.build(
                dataset_rid=dr, transaction_rid=tr, request_timeout=timeout
            )
        if operation == "commit":
            return await client.commit(
                dataset_rid=dr, transaction_rid=tr, request_timeout=timeout
            )
        if operation == "create":
            return await client.create(
                dataset_rid=dr,
                transaction_type=args.transaction_type,
                branch_name=bn,
                request_timeout=timeout,
            )
        if operation == "get":
            return await client.get(
                dataset_rid=dr, transaction_rid=tr, request_timeout=timeout
            )
        if operation == "job":
            return await client.job(
                dataset_rid=dr, transaction_rid=tr, request_timeout=timeout
            )
    elif resource == "view":
        bd_arg = getattr(args, "backing_datasets", None)
        bd = json.loads(bd_arg) if isinstance(bd_arg, str) else bd_arg
        pk_arg = getattr(args, "primary_key", None)
        pk = json.loads(pk_arg) if isinstance(pk_arg, str) else pk_arg
        if operation == "add_backing_datasets":
            return await client.add_backing_datasets(
                view_dataset_rid=vr,
                backing_datasets=bd,
                branch=br,
                request_timeout=timeout,
            )
        if operation == "add_primary_key":
            return await client.add_primary_key(
                view_dataset_rid=vr, primary_key=pk, branch=br, request_timeout=timeout
            )
        if operation == "create":
            return await client.create(
                view_name=args.view_name,
                parent_folder_rid=pfr,
                backing_datasets=bd,
                branch=br,
                primary_key=pk,
                request_timeout=timeout,
            )
        if operation == "get":
            return await client.get(
                view_dataset_rid=vr, branch=br, request_timeout=timeout
            )
        if operation == "remove_backing_datasets":
            return await client.remove_backing_datasets(
                view_dataset_rid=vr,
                backing_datasets=bd,
                branch=br,
                request_timeout=timeout,
            )
        if operation == "replace_backing_datasets":
            return await client.replace_backing_datasets(
                view_dataset_rid=vr,
                backing_datasets=bd,
                branch=br,
                request_timeout=timeout,
            )
    raise ValueError(f"Unknown operation: {resource}.{operation}")


async def _download_file_content(
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
    cfg: ConfigLoader,
) -> dict[str, Any]:
    """Stream Dataset file content into a bounded, atomic download."""
    response_context = client.with_streaming_response.content(
        dataset_rid=args.dataset_rid,
        file_path=args.file_path,
        branch_name=args.branch_name,
        end_transaction_rid=args.end_transaction_rid,
        start_transaction_rid=args.start_transaction_rid,
        request_timeout=timeout,
    )
    async with response_context as response:
        result = await BinaryDownloadHandler(config=cfg).save(
            response.aiter_bytes(),
            original_filename=args.output,
            namespace="datasets",
            operation="file.content",
        )
    return result.to_dict()


async def _download_read_table(
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
    cfg: ConfigLoader,
) -> dict[str, Any]:
    """Stream a Dataset table export into a bounded, atomic download."""
    columns = None
    if args.columns_json is not None:
        try:
            columns = json.loads(args.columns_json)
        except json.JSONDecodeError as exc:
            raise ValueError("--columns-json must be a JSON array of strings") from exc
        if not isinstance(columns, list) or not all(
            isinstance(column, str) for column in columns
        ):
            raise ValueError("--columns-json must be a JSON array of strings")

    response_context = client.with_streaming_response.read_table(
        dataset_rid=args.dataset_rid,
        format=args.table_format,
        branch_name=args.branch_name,
        columns=columns,
        end_transaction_rid=args.end_transaction_rid,
        row_limit=args.row_limit,
        start_transaction_rid=args.start_transaction_rid,
        request_timeout=timeout,
    )
    async with response_context as response:
        result = await BinaryDownloadHandler(config=cfg).save(
            response.aiter_bytes(),
            original_filename=args.output,
            namespace="datasets",
            operation="dataset.read_table",
            mime_type="text/csv" if args.table_format == "CSV" else None,
        )
    return result.to_dict()


# Operations that return a paginated response and accept page_size/page_token
# (FR-PAG-3). For these, the CLI wraps the SDK call with PaginationHelper to
# honour --page-size / --page-token / --batch-pages and emit metadata to stderr.
PAGINATED_OPS = frozenset(
    {
        ("dataset", "get_schedules"),
        ("dataset", "jobs"),
        ("dataset", "transactions"),
        ("branch", "list"),
        ("branch", "transactions"),
        ("file", "list"),
    }
)


def _is_paginated(resource: str, operation: str) -> bool:
    """Return True if the operation supports pagination (FR-PAG-3)."""
    return (resource, operation) in PAGINATED_OPS


async def _invoke_paginated(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
    helper: PaginationHelper,
) -> Any:
    """Fetch actual SDK response pages and preserve their continuation tokens.

    Ordinary SDK list methods return ``AsyncResourceIterator``, which cannot be
    awaited and exposes no public page cursor. Raw response methods return one
    decodable page, so the shared helper can honor CLI paging options.
    """

    raw_client = client.with_raw_response

    async def _single_page(**page_kwargs: Any) -> Any:
        # Materialize a fresh namespace so page tokens from the helper override
        # the CLI-supplied values without mutating the original args.
        paged_args = argparse.Namespace(**vars(args))
        paged_args.page_size = page_kwargs.get(
            "page_size", getattr(args, "page_size", None)
        )
        paged_args.page_token = page_kwargs.get("page_token", None)
        raw_response = await _invoke(
            resource, operation, raw_client, paged_args, timeout
        )
        page = raw_response.decode()
        return {
            "items": list(getattr(page, "data", None) or []),
            "next_page_token": getattr(page, "next_page_token", None),
        }

    return await helper.paginate(_single_page)


OP_MAP = {
    "dataset": {
        "get-health-check-reports": "get_health_check_reports",
        "get-health-checks": "get_health_checks",
        "get-schedules": "get_schedules",
        "get-schema": "get_schema",
        "get-schema-batch": "get_schema_batch",
        "put-schema": "put_schema",
        "read-table": "read_table",
    },
    "view": {
        "add-backing-datasets": "add_backing_datasets",
        "add-primary-key": "add_primary_key",
        "remove-backing-datasets": "remove_backing_datasets",
        "replace-backing-datasets": "replace_backing_datasets",
    },
}


def _resolve(resource: str, op: str) -> str:
    """Resolve kebab-case operation name to snake_case."""
    mapping = OP_MAP.get(resource, {})
    return mapping.get(op, op.replace("-", "_"))


def _common_parser(*, paginated: bool = True) -> argparse.ArgumentParser:
    """Build the shared argparse parent with global options.

    Every leaf operation subparser inherits these arguments via ``parents``,
    so options like ``--timeout`` may appear after the operation positional.
    """
    common = _ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=None)
    common.add_argument("--format", choices=["json", "toon", "auto"], default=None)
    common.add_argument("--pretty", action="store_true")
    if paginated:
        common.add_argument("--page-size", type=int, default=None, dest="page_size")
        common.add_argument("--page-token", type=str, default=None, dest="page_token")
        common.add_argument("--batch-pages", type=int, default=None, dest="batch_pages")
    return common


def _add_operation(
    subparsers: Any,
    name: str,
    *arguments: tuple[tuple[str, ...], dict[str, Any]],
    paginated: bool = True,
) -> argparse.ArgumentParser:
    operation_parser = subparsers.add_parser(
        name, parents=[_common_parser(paginated=paginated)]
    )
    for flags, kwargs in arguments:
        operation_parser.add_argument(*flags, **kwargs)
    return cast(argparse.ArgumentParser, operation_parser)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 33 operations."""
    parser = _ArgumentParser(
        prog="pal_found_datasets_cli",
        description="Foundry Datasets CLI - 33 operations across 5 resource clients",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    ds = subparsers.add_parser("dataset")
    ds_sub = ds.add_subparsers(dest="operation")
    _add_operation(
        ds_sub,
        "create",
        (("--name",), {"required": True}),
        (("--parent-folder-rid",), {"required": True, "dest": "parent_folder_rid"}),
    )
    _add_operation(ds_sub, "get", (("dataset_rid",), {}))
    for operation_name in (
        "get-health-check-reports",
        "get-health-checks",
        "get-schedules",
        "get-schema",
    ):
        _add_operation(
            ds_sub,
            operation_name,
            (("dataset_rid",), {}),
            (("--branch-name",), {"default": None, "dest": "branch_name"}),
        )
    _add_operation(
        ds_sub,
        "read-table",
        (("dataset_rid",), {}),
        (
            ("--table-format",),
            {"required": True, "choices": ("ARROW", "CSV"), "dest": "table_format"},
        ),
        (("--output",), {"default": None}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
        (("--columns-json",), {"default": None, "dest": "columns_json"}),
        (("--end-transaction-rid",), {"default": None, "dest": "end_transaction_rid"}),
        (("--row-limit",), {"type": int, "default": None, "dest": "row_limit"}),
        (
            ("--start-transaction-rid",),
            {"default": None, "dest": "start_transaction_rid"},
        ),
        paginated=False,
    )
    _add_operation(
        ds_sub,
        "get-schema-batch",
        (("--body-json",), {"required": True, "dest": "body_json"}),
    )
    _add_operation(
        ds_sub,
        "jobs",
        (("dataset_rid",), {}),
        (
            ("--order-by-json",),
            {
                "default": '[{"sortType":"BY_STARTED_TIME","sortDirection":"DESCENDING"}]',
                "dest": "order_by_json",
            },
        ),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
    )
    _add_operation(
        ds_sub,
        "put-schema",
        (("dataset_rid",), {}),
        (("--schema",), {"required": True}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
    )
    _add_operation(ds_sub, "transactions", (("dataset_rid",), {}))

    br = subparsers.add_parser("branch")
    br_sub = br.add_subparsers(dest="operation")
    _add_operation(
        br_sub,
        "create",
        (("dataset_rid",), {}),
        (("--name",), {"required": True}),
        (("--transaction-rid",), {"default": None, "dest": "transaction_rid"}),
    )
    _add_operation(
        br_sub,
        "delete",
        (("dataset_rid",), {}),
        (("--branch-name",), {"required": True, "dest": "branch_name"}),
    )
    for operation_name in ("get", "transactions"):
        _add_operation(
            br_sub,
            operation_name,
            (("dataset_rid",), {}),
            (("--branch-name",), {"default": None, "dest": "branch_name"}),
        )
    _add_operation(br_sub, "list", (("dataset_rid",), {}))

    fl = subparsers.add_parser("file")
    fl_sub = fl.add_subparsers(dest="operation")
    _add_operation(
        fl_sub,
        "content",
        (("dataset_rid",), {}),
        (("--file-path",), {"required": True, "dest": "file_path"}),
        (("--output",), {"default": None}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
        (("--end-transaction-rid",), {"default": None, "dest": "end_transaction_rid"}),
        (
            ("--start-transaction-rid",),
            {"default": None, "dest": "start_transaction_rid"},
        ),
    )
    for operation_name in ("delete", "upload"):
        _add_operation(
            fl_sub,
            operation_name,
            (("dataset_rid",), {}),
            (("--file-path",), {"required": True, "dest": "file_path"}),
            (("--transaction-rid",), {"default": None, "dest": "transaction_rid"}),
        )
    _add_operation(
        fl_sub,
        "get",
        (("dataset_rid",), {}),
        (("--file-path",), {"required": True, "dest": "file_path"}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
        (("--end-transaction-rid",), {"default": None, "dest": "end_transaction_rid"}),
        (
            ("--start-transaction-rid",),
            {"default": None, "dest": "start_transaction_rid"},
        ),
    )
    _add_operation(
        fl_sub,
        "list",
        (("dataset_rid",), {}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
        (("--end-transaction-rid",), {"default": None, "dest": "end_transaction_rid"}),
        (("--path-prefix",), {"default": None, "dest": "path_prefix"}),
        (
            ("--start-transaction-rid",),
            {"default": None, "dest": "start_transaction_rid"},
        ),
    )

    tx = subparsers.add_parser("transaction")
    tx_sub = tx.add_subparsers(dest="operation")
    for operation_name in ("abort", "build", "commit", "get", "job"):
        _add_operation(
            tx_sub,
            operation_name,
            (("dataset_rid",), {}),
            (("--transaction-rid",), {"required": True, "dest": "transaction_rid"}),
        )
    _add_operation(
        tx_sub,
        "create",
        (("dataset_rid",), {}),
        (("--transaction-type",), {"required": True, "dest": "transaction_type"}),
        (("--branch-name",), {"default": None, "dest": "branch_name"}),
    )

    vw = subparsers.add_parser("view")
    vw_sub = vw.add_subparsers(dest="operation")
    for operation_name in (
        "add-backing-datasets",
        "remove-backing-datasets",
        "replace-backing-datasets",
    ):
        _add_operation(
            vw_sub,
            operation_name,
            (("--view-dataset-rid",), {"required": True, "dest": "view_dataset_rid"}),
            (("--backing-datasets",), {"required": True, "dest": "backing_datasets"}),
            (("--branch",), {"default": None}),
        )
    _add_operation(
        vw_sub,
        "add-primary-key",
        (("--view-dataset-rid",), {"required": True, "dest": "view_dataset_rid"}),
        (("--primary-key",), {"required": True, "dest": "primary_key"}),
        (("--branch",), {"default": None}),
    )
    _add_operation(
        vw_sub,
        "create",
        (("--view-name",), {"required": True, "dest": "view_name"}),
        (("--parent-folder-rid",), {"required": True, "dest": "parent_folder_rid"}),
        (("--backing-datasets",), {"required": True, "dest": "backing_datasets"}),
        (("--branch",), {"default": None}),
        (("--primary-key",), {"default": None, "dest": "primary_key"}),
    )
    _add_operation(
        vw_sub,
        "get",
        (("--view-dataset-rid",), {"required": True, "dest": "view_dataset_rid"}),
        (("--branch",), {"default": None}),
    )

    return parser


async def async_main() -> int:
    """Main entry point: async, returns exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("An operation is required")
    except CLIInputError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_USER_INPUT

    cfg = ConfigLoader()
    cfg.load()

    # Initialize logging (ADR-005)
    LogSetup.configure(log_level=cfg.log_level)

    # Resolve operation name
    resource = args.resource
    operation = _resolve(resource, args.operation)
    logger.info(
        "Executing operation", extra={"resource": resource, "operation": operation}
    )

    # Access control check (ADR-007)
    guard = AccessControlGuard(cfg, "DATASETS")
    try:
        guard.check(resource, operation)
    except AccessControlError as exc:
        logger.warning("Access control denied", extra={"error": str(exc)})
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_ACCESS_CONTROL

    try:
        timeout = resolve_timeout(args.timeout, default=cfg.timeout_s)
    except ValueError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_USER_INPUT

    # Create SDK client
    try:
        client = _get_client(cfg, resource)
    except Exception as exc:
        logger.error("Failed to create client", extra={"error": str(exc)})
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_CONFIGURATION

    # WARNING-2 fix: resolve timeout from CLI flag or config default.
    # cfg.timeout_s is the project-wide default (ADR-002). CLI flag wins.

    # WARNING-1 fix: apply RetryHandler for transient error recovery (ADR-002).
    retry_handler = RetryHandler(timeout_s=timeout)

    # Build PaginationHelper when the operation is paginated (FR-PAG).
    # --page-size / --page-token / --batch-pages are parsed by _common_parser.
    helper: PaginationHelper | None = None
    if _is_paginated(resource, operation):
        helper = PaginationHelper(
            page_size=getattr(args, "page_size", None),
            page_token=getattr(args, "page_token", None),
            batch_pages=getattr(args, "batch_pages", None),
        )

    # Invoke operation
    try:
        if resource == "file" and operation == "content":
            result = await retry_handler.execute(
                _download_file_content, client, args, timeout, cfg
            )
        elif resource == "dataset" and operation == "read_table":
            result = await retry_handler.execute(
                _download_read_table, client, args, timeout, cfg
            )
        elif helper is not None:
            result = await retry_handler.execute(
                _invoke_paginated,
                resource,
                operation,
                client,
                args,
                timeout,
                helper,
            )
        else:
            result = await retry_handler.execute(
                _invoke, resource, operation, client, args, timeout
            )
        formatter = OutputFormatter(
            format_setting=getattr(args, "format", None),
            pretty=getattr(args, "pretty", False),
        )
        output = formatter.format(_model_to_dict(result))
        print(output)
        # Emit pagination metadata to stderr after stdout (ADR-005, FR-PAG-2).
        if helper is not None:
            helper.emit_metadata()
        return EXIT_SUCCESS
    except AccessControlError as exc:
        logger.warning("Access control denied", extra={"error": str(exc)})
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_ACCESS_CONTROL
    except PermissionError as exc:
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_PERMISSION_DENIED
    except FileNotFoundError as exc:
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_NOT_FOUND
    except DownloadError as exc:
        serializer = ErrorSerializer()
        return serializer.serialize(exc)
    except ValueError as exc:
        serializer = ErrorSerializer()
        return serializer.serialize(exc)
    except TimeoutError as exc:
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_TIMEOUT
    except OSError as exc:
        serializer = ErrorSerializer()
        exit_code = serializer.serialize(exc)
        # Check if it looks like a rate limit (errno 11 or EAGAIN)
        if getattr(exc, "errno", None) in (11, 115):
            return EXIT_RATE_LIMIT
        return exit_code
    except Exception as exc:
        serializer = ErrorSerializer()
        serializer.serialize(exc)
        return EXIT_SERVER_ERROR


def main() -> int:
    """Run the async CLI from the console script entry point."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
