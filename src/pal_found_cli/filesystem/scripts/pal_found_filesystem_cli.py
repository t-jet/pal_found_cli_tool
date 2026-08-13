#!/usr/bin/env python3
"""Foundry Filesystem CLI - 31 canonical API v2 operations."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT: str | None = None
_candidate = _SCRIPT_DIR
for _depth in range(8):
    if (_candidate / "src" / "pal_found_cli" / "__init__.py").exists():
        _PROJECT_ROOT = str(_candidate)
        break
    if _candidate.parent == _candidate:
        break
    _candidate = _candidate.parent
if _PROJECT_ROOT is None:
    _PROJECT_ROOT = str(_SCRIPT_DIR.parents[4])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from pal_found_cli.common.access_control_guard import (
    AccessControlError,
    AccessControlGuard,
)
from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.common.config_loader import ConfigLoader
from pal_found_cli.common.error_serializer import (
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
from pal_found_cli.common.log_setup import LogSetup
from pal_found_cli.common.output_formatter import OutputFormatter
from pal_found_cli.common.pagination_helper import PaginationHelper
from pal_found_cli.common.retry import RetryHandler

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
        "folder",
        "children",
        "Folder",
        "children",
        ("folder_rid",),
        optional=("page_size", "page_token"),
    ),
    _op(
        "folder",
        "create",
        "Folder",
        "create",
        required=("display_name", "parent_folder_rid"),
    ),
    _op("folder", "get", "Folder", "get", ("folder_rid",)),
    _op(
        "folder",
        "get_batch",
        "Folder",
        "get_batch",
        ("body",),
        json_args=("body",),
    ),
    _op(
        "folder",
        "replace",
        "Folder",
        "replace",
        ("folder_rid",),
        required=("display_name", "parent_folder_rid"),
        optional=("preview",),
    ),
    _op(
        "project",
        "add_organizations",
        "Project",
        "add_organizations",
        ("project_rid",),
        required=("organization_rids",),
        json_args=("organization_rids",),
    ),
    _op(
        "project",
        "create",
        "Project",
        "create",
        required=(
            "default_roles",
            "display_name",
            "organization_rids",
            "role_grants",
            "space_rid",
        ),
        optional=("description", "resource_level_role_grants_allowed"),
        json_args=("default_roles", "organization_rids", "role_grants"),
    ),
    _op(
        "project",
        "create_from_template",
        "Project",
        "create_from_template",
        required=("template_rid", "variable_values"),
        optional=("default_roles", "organization_rids", "project_description"),
        json_args=("variable_values", "default_roles", "organization_rids"),
    ),
    _op("project", "get", "Project", "get", ("project_rid",)),
    _op(
        "project",
        "organizations",
        "Project",
        "organizations",
        ("project_rid",),
        optional=("page_size", "page_token"),
    ),
    _op(
        "project",
        "remove_organizations",
        "Project",
        "remove_organizations",
        ("project_rid",),
        required=("organization_rids",),
        json_args=("organization_rids",),
    ),
    _op(
        "project",
        "replace",
        "Project",
        "replace",
        ("project_rid",),
        required=("display_name",),
        optional=("description", "preview"),
    ),
    _op(
        "resource",
        "add_markings",
        "Resource",
        "add_markings",
        ("resource_rid",),
        required=("marking_ids",),
        json_args=("marking_ids",),
    ),
    _op("resource", "delete", "Resource", "delete", ("resource_rid",)),
    _op("resource", "get", "Resource", "get", ("resource_rid",)),
    _op(
        "resource",
        "get_access_requirements",
        "Resource",
        "get_access_requirements",
        ("resource_rid",),
    ),
    _op(
        "resource",
        "get_batch",
        "Resource",
        "get_batch",
        ("body",),
        json_args=("body",),
    ),
    _op(
        "resource",
        "get_by_path",
        "Resource",
        "get_by_path",
        required=("path",),
    ),
    _op(
        "resource",
        "get_by_path_batch",
        "Resource",
        "get_by_path_batch",
        ("body",),
        json_args=("body",),
    ),
    _op(
        "resource",
        "markings",
        "Resource",
        "markings",
        ("resource_rid",),
        optional=("page_size", "page_token"),
    ),
    _op(
        "resource",
        "permanently_delete",
        "Resource",
        "permanently_delete",
        ("resource_rid",),
    ),
    _op(
        "resource",
        "remove_markings",
        "Resource",
        "remove_markings",
        ("resource_rid",),
        required=("marking_ids",),
        json_args=("marking_ids",),
    ),
    _op("resource", "restore", "Resource", "restore", ("resource_rid",)),
    _op(
        "resource_role",
        "add",
        "Resource.Role",
        "add",
        ("resource_rid",),
        required=("roles",),
        json_args=("roles",),
    ),
    _op(
        "resource_role",
        "list",
        "Resource.Role",
        "list",
        ("resource_rid",),
        optional=("include_inherited", "page_size", "page_token"),
    ),
    _op(
        "resource_role",
        "remove",
        "Resource.Role",
        "remove",
        ("resource_rid",),
        required=("roles",),
        json_args=("roles",),
    ),
    _op(
        "space",
        "create",
        "Space",
        "create",
        required=(
            "deletion_policy_organizations",
            "display_name",
            "enrollment_rid",
            "organizations",
        ),
        optional=(
            "default_role_set_id",
            "description",
            "file_system_id",
            "preview",
            "usage_account_rid",
        ),
        json_args=("deletion_policy_organizations", "organizations"),
    ),
    _op("space", "delete", "Space", "delete", ("space_rid",), optional=("preview",)),
    _op("space", "get", "Space", "get", ("space_rid",), optional=("preview",)),
    _op("space", "list", "Space", "list", optional=("page_size", "page_token")),
    _op(
        "space",
        "replace",
        "Space",
        "replace",
        ("space_rid",),
        required=("display_name",),
        optional=("default_role_set_id", "description", "preview", "usage_account_rid"),
    ),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_spec["resource"], {})[_spec["operation"]] = _spec

PAGINATED_OPS = frozenset(
    {
        ("folder", "children"),
        ("project", "organizations"),
        ("resource", "markings"),
        ("resource_role", "list"),
        ("space", "list"),
    }
)


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
    common.add_argument("--page-size", type=int, default=None, dest="page_size")
    common.add_argument("--page-token", type=str, default=None, dest="page_token")
    common.add_argument("--batch-pages", type=int, default=None, dest="batch_pages")
    return common


def _add_kwarg(
    parser: argparse.ArgumentParser, arg_name: str, *, required: bool
) -> None:
    flag = "--" + _kebab(arg_name)
    if arg_name in {
        "include_inherited",
        "preview",
        "resource_level_role_grants_allowed",
    }:
        parser.add_argument(flag, action="store_true", default=None, dest=arg_name)
        return
    parser.add_argument(flag, required=required, default=None, dest=arg_name)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 31 filesystem operations."""
    parser = argparse.ArgumentParser(
        prog="pal_found_filesystem_cli",
        description="Foundry Filesystem CLI - 31 operations",
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
                if arg_name in {"page_size", "page_token"}:
                    continue
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
    """Get SDK client for a filesystem resource."""
    root = (factory or AsyncClientFactory()).create(cfg).filesystem
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


def _is_async_iterator(value: Any) -> bool:
    """Return true for SDK async resource iterators."""
    return hasattr(value, "__aiter__") and hasattr(value, "__anext__")


async def _read_next_page_token(iterator: Any) -> str | None:
    """Read the SDK async iterator cursor when the implementation exposes it."""
    page_iterator = getattr(iterator, "_page_iterator", None)
    token_source = page_iterator if page_iterator is not None else iterator
    getter = getattr(token_source, "get_next_page_token", None)
    if callable(getter):
        token = await _resolve_result(getter())
        return token if isinstance(token, str) else None
    token = getattr(token_source, "next_page_token", None)
    return token if isinstance(token, str) else None


async def _collect_async_iterator(iterator: Any, helper: PaginationHelper) -> list[Any]:
    """Collect items from a Foundry SDK AsyncResourceIterator."""
    items: list[Any] = []
    item_limit = helper.page_size * helper.batch_pages

    async for item in iterator:
        items.append(item)
        if len(items) >= item_limit:
            break

    helper._total_items += len(items)
    helper._pages_fetched += max(1, math.ceil(len(items) / helper.page_size))
    helper._next_page_token = await _read_next_page_token(iterator)
    return items


async def _paginate_page_envelopes(
    first_response: Any,
    call_func: Any,
    helper: PaginationHelper,
) -> list[Any]:
    """Collect dict/list page envelopes used by older tests and some SDK methods."""
    all_items = helper._extract_items(first_response)
    helper._total_items += len(all_items)
    helper._pages_fetched += 1
    token = helper._extract_next_token(first_response)

    while token is not None and helper.pages_fetched < helper.batch_pages:
        response = await call_func(page_size=helper.page_size, page_token=token)
        items = helper._extract_items(response)
        all_items.extend(items)
        helper._total_items += len(items)
        helper._pages_fetched += 1
        token = helper._extract_next_token(response)

    helper._next_page_token = token
    return all_items


async def _invoke(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
) -> Any:
    """Invoke a filesystem SDK operation."""
    spec = _spec_for(resource, operation)
    method = getattr(client, spec["method"])
    positional = [getattr(args, name) for name in spec["positional"]]
    kwargs: dict[str, Any] = {}

    for name in (*spec["required"], *spec["optional"]):
        if name in {"page_size", "page_token"}:
            value = getattr(args, name, None)
        else:
            value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = _coerce_arg(value, json_arg=name in spec["json_args"])

    kwargs["request_timeout"] = timeout
    return await _resolve_result(method(*positional, **kwargs))


def _is_paginated(resource: str, operation: str) -> bool:
    """Return true if operation supports SDK pagination args."""
    return (resource, operation) in PAGINATED_OPS


async def _invoke_paginated(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: int | None,
    helper: PaginationHelper,
) -> Any:
    """Invoke a paginated filesystem operation."""

    async def _single_page(**page_kwargs: Any) -> Any:
        paged_args = argparse.Namespace(**vars(args))
        paged_args.page_size = page_kwargs.get(
            "page_size", getattr(args, "page_size", None)
        )
        paged_args.page_token = page_kwargs.get("page_token", None)
        return await _invoke(resource, operation, client, paged_args, timeout)

    first_response = await _single_page(**helper.get_sdk_params())
    if _is_async_iterator(first_response):
        return await _collect_async_iterator(first_response, helper)
    return await _paginate_page_envelopes(first_response, _single_page, helper)


async def main() -> int:
    """Run the Foundry Filesystem CLI."""
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
        AccessControlGuard(cfg, "FILESYSTEM").check(resource, operation)
    except AccessControlError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_ACCESS_CONTROL

    factory = AsyncClientFactory()
    try:
        with factory.invocation_scope(cfg):
            client = _get_client(cfg, resource, factory)
            timeout = getattr(args, "timeout", None) or getattr(cfg, "timeout_s", None)
            retry_handler = RetryHandler()
            helper: PaginationHelper | None = None
            if _is_paginated(resource, operation):
                helper = PaginationHelper(
                    page_size=getattr(args, "page_size", None),
                    page_token=getattr(args, "page_token", None),
                    batch_pages=getattr(args, "batch_pages", None),
                )
            if helper is not None:
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
        if helper is not None:
            helper.emit_metadata()
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
