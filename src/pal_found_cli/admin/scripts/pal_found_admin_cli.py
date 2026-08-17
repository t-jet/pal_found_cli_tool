#!/usr/bin/env python3
"""Foundry Admin CLI - 66 canonical API v2 operations."""

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

from pal_found_cli.common.access_control_guard import (  # noqa: E402
    AccessControlError,
    AccessControlGuard,
)
from pal_found_cli.common.async_client_factory import AsyncClientFactory  # noqa: E402
from pal_found_cli.common.config_loader import ConfigLoader  # noqa: E402
from pal_found_cli.common.error_serializer import (  # noqa: E402
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
from pal_found_cli.common.log_setup import LogSetup  # noqa: E402
from pal_found_cli.common.output_formatter import OutputFormatter  # noqa: E402
from pal_found_cli.common.pagination_helper import PaginationHelper  # noqa: E402
from pal_found_cli.common.retry import RetryHandler  # noqa: E402

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]

JSON_ARGS = frozenset(
    {
        "attributes",
        "administrators",
        "body",
        "initial_members",
        "initial_permissions",
        "initial_role_assignments",
        "marking_ids",
        "organizations",
        "principal_ids",
        "role_assignments",
        "where",
    }
)
BOOLEAN_ARGS = frozenset({"include_expirations", "preview", "transitive"})


def _op(
    resource: str,
    operation: str,
    client_path: str,
    method: str,
    positional: Iterable[str] = (),
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
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
        "json_args": frozenset(
            name
            for name in (*positional, *required, *optional)
            if name in JSON_ARGS
        ),
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op(
        "authentication_provider",
        "get",
        "Enrollment.AuthenticationProvider",
        "get",
        ("enrollment_rid", "authentication_provider_rid"),
        optional=("preview",),
    ),
    _op(
        "authentication_provider",
        "list",
        "Enrollment.AuthenticationProvider",
        "list",
        ("enrollment_rid",),
        optional=("preview",),
    ),
    _op(
        "authentication_provider",
        "preregister_group",
        "Enrollment.AuthenticationProvider",
        "preregister_group",
        ("enrollment_rid", "authentication_provider_rid"),
        required=("name", "organizations"),
        optional=("preview",),
    ),
    _op(
        "authentication_provider",
        "preregister_user",
        "Enrollment.AuthenticationProvider",
        "preregister_user",
        ("enrollment_rid", "authentication_provider_rid"),
        required=("organization", "username"),
        optional=("attributes", "email", "family_name", "given_name", "preview"),
    ),
    _op(
        "cbac_banner",
        "get",
        "CbacBanner",
        "get",
        optional=("display_type", "marking_ids", "preview"),
    ),
    _op(
        "cbac_marking_restrictions",
        "get",
        "CbacMarkingRestrictions",
        "get",
        optional=("marking_ids", "preview"),
    ),
    _op("enrollment", "get", "Enrollment", "get", ("enrollment_rid",), optional=("preview",)),
    _op("enrollment", "get_current", "Enrollment", "get_current", optional=("preview",)),
    _op(
        "enrollment_role_assignment",
        "add",
        "Enrollment.EnrollmentRoleAssignment",
        "add",
        ("enrollment_rid",),
        required=("role_assignments",),
        optional=("preview",),
    ),
    _op(
        "enrollment_role_assignment",
        "list",
        "Enrollment.EnrollmentRoleAssignment",
        "list",
        ("enrollment_rid",),
        optional=("preview",),
    ),
    _op(
        "enrollment_role_assignment",
        "remove",
        "Enrollment.EnrollmentRoleAssignment",
        "remove",
        ("enrollment_rid",),
        required=("role_assignments",),
        optional=("preview",),
    ),
    _op("group", "create", "Group", "create", required=("attributes", "name", "organizations"), optional=("description",)),
    _op("group", "delete", "Group", "delete", ("group_id",)),
    _op("group", "get", "Group", "get", ("group_id",)),
    _op("group", "get_batch", "Group", "get_batch", ("body",)),
    _op("group", "list", "Group", "list", optional=("page_size", "page_token")),
    _op("group", "list_current", "Group", "list_current", optional=("preview",)),
    _op("group", "replace", "Group", "replace", ("group_id",), required=("attributes", "name", "organizations"), optional=("description",)),
    _op("group", "search", "Group", "search", required=("where",), optional=("page_size", "page_token")),
    _op("group_member", "add", "Group.GroupMember", "add", ("group_id",), required=("principal_ids",), optional=("expiration",)),
    _op("group_member", "list", "Group.GroupMember", "list", ("group_id",), optional=("include_expirations", "page_size", "page_token", "transitive")),
    _op("group_member", "remove", "Group.GroupMember", "remove", ("group_id",), required=("principal_ids",)),
    _op("group_membership", "list", "User.GroupMembership", "list", ("user_id",), optional=("page_size", "page_token", "transitive")),
    _op("group_membership_expiration_policy", "get", "Group.MembershipExpirationPolicy", "get", ("group_id",), optional=("preview",)),
    _op("group_membership_expiration_policy", "replace", "Group.MembershipExpirationPolicy", "replace", ("group_id",), optional=("maximum_duration", "maximum_value", "preview")),
    _op("group_provider_info", "get", "Group.ProviderInfo", "get", ("group_id",)),
    _op("group_provider_info", "replace", "Group.ProviderInfo", "replace", ("group_id",), required=("provider_id",)),
    _op("host", "list", "Enrollment.Host", "list", ("enrollment_rid",), optional=("page_size", "page_token", "preview")),
    _op("marking", "create", "Marking", "create", required=("category_id", "initial_members", "initial_role_assignments", "name"), optional=("description",)),
    _op("marking", "get", "Marking", "get", ("marking_id",)),
    _op("marking", "get_batch", "Marking", "get_batch", ("body",)),
    _op("marking", "list", "Marking", "list", optional=("page_size", "page_token")),
    _op("marking", "replace", "Marking", "replace", ("marking_id",), required=("name",), optional=("description",)),
    _op("marking_category", "create", "MarkingCategory", "create", required=("description", "initial_permissions", "name"), optional=("preview",)),
    _op("marking_category", "get", "MarkingCategory", "get", ("marking_category_id",)),
    _op("marking_category", "list", "MarkingCategory", "list", optional=("page_size", "page_token")),
    _op("marking_category", "replace", "MarkingCategory", "replace", ("marking_category_id",), required=("description", "name"), optional=("preview",)),
    _op("marking_member", "add", "Marking.MarkingMember", "add", ("marking_id",), required=("principal_ids",)),
    _op("marking_member", "list", "Marking.MarkingMember", "list", ("marking_id",), optional=("page_size", "page_token", "transitive")),
    _op("marking_member", "remove", "Marking.MarkingMember", "remove", ("marking_id",), required=("principal_ids",)),
    _op("marking_role_assignment", "add", "Marking.MarkingRoleAssignment", "add", ("marking_id",), required=("role_assignments",)),
    _op("marking_role_assignment", "list", "Marking.MarkingRoleAssignment", "list", ("marking_id",), optional=("page_size", "page_token")),
    _op("marking_role_assignment", "remove", "Marking.MarkingRoleAssignment", "remove", ("marking_id",), required=("role_assignments",)),
    _op("organization", "create", "Organization", "create", required=("administrators", "enrollment_rid", "name"), optional=("description", "host", "preview")),
    _op("organization", "get", "Organization", "get", ("organization_rid",)),
    _op("organization", "list_available_roles", "Organization", "list_available_roles", ("organization_rid",)),
    _op("organization", "replace", "Organization", "replace", ("organization_rid",), required=("name",), optional=("description", "host")),
    _op("organization_guest_member", "add", "Organization.OrganizationGuestMember", "add", ("organization_rid",), required=("principal_ids",), optional=("preview",)),
    _op("organization_guest_member", "list", "Organization.OrganizationGuestMember", "list", ("organization_rid",), optional=("preview",)),
    _op("organization_guest_member", "remove", "Organization.OrganizationGuestMember", "remove", ("organization_rid",), required=("principal_ids",), optional=("preview",)),
    _op("organization_role_assignment", "add", "Organization.OrganizationRoleAssignment", "add", ("organization_rid",), required=("role_assignments",)),
    _op("organization_role_assignment", "list", "Organization.OrganizationRoleAssignment", "list", ("organization_rid",)),
    _op("organization_role_assignment", "remove", "Organization.OrganizationRoleAssignment", "remove", ("organization_rid",), required=("role_assignments",)),
    _op("role", "get", "Role", "get", ("role_id",), optional=("preview",)),
    _op("role", "get_batch", "Role", "get_batch", ("body",), optional=("preview",)),
    _op("user", "delete", "User", "delete", ("user_id",)),
    _op("user", "get", "User", "get", ("user_id",), optional=("status",)),
    _op("user", "get_batch", "User", "get_batch", ("body",)),
    _op("user", "get_current", "User", "get_current"),
    _op("user", "get_markings", "User", "get_markings", ("user_id",)),
    _op("user", "list", "User", "list", optional=("include", "page_size", "page_token")),
    _op("user", "profile_picture", "User", "profile_picture", ("user_id",)),
    _op("user", "revoke_all_tokens", "User", "revoke_all_tokens", ("user_id",)),
    _op("user", "search", "User", "search", required=("where",), optional=("page_size", "page_token")),
    _op("user_provider_info", "get", "User.ProviderInfo", "get", ("user_id",)),
    _op("user_provider_info", "replace", "User.ProviderInfo", "replace", ("user_id",), required=("provider_id",)),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_spec["resource"], {})[_spec["operation"]] = _spec

PAGINATED_OPS = frozenset(
    {
        ("group", "list"),
        ("group", "search"),
        ("group_member", "list"),
        ("group_membership", "list"),
        ("host", "list"),
        ("marking", "list"),
        ("marking_category", "list"),
        ("marking_member", "list"),
        ("marking_role_assignment", "list"),
        ("user", "list"),
        ("user", "search"),
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


def _common_parser(*, paginated: bool) -> argparse.ArgumentParser:
    """Build shared parser for operation-level common options."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=None)
    common.add_argument("--format", choices=["json", "toon", "auto"], default="auto")
    common.add_argument("--pretty", action="store_true")
    if paginated:
        common.add_argument("--page-size", type=int, default=None, dest="page_size")
        common.add_argument("--page-token", type=str, default=None, dest="page_token")
        common.add_argument("--batch-pages", type=int, default=None, dest="batch_pages")
    return common


def _add_kwarg(
    parser: argparse.ArgumentParser, arg_name: str, *, required: bool
) -> None:
    """Add a named operation argument to the parser."""
    flag = "--" + _kebab(arg_name)
    if arg_name in BOOLEAN_ARGS:
        parser.add_argument(flag, action="store_true", default=None, dest=arg_name)
        return
    parser.add_argument(flag, required=required, default=None, dest=arg_name)


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 66 admin operations."""
    parser = argparse.ArgumentParser(
        prog="pal_found_admin_cli",
        description="Foundry Admin CLI - 66 operations",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    for resource in sorted(OPERATION_BY_RESOURCE):
        res_parser = subparsers.add_parser(_kebab(resource))
        op_sub = res_parser.add_subparsers(dest="operation")
        for operation, spec in sorted(OPERATION_BY_RESOURCE[resource].items()):
            is_paginated = _is_paginated(resource, operation)
            op_parser = op_sub.add_parser(
                _kebab(operation), parents=[_common_parser(paginated=is_paginated)]
            )
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
    """Get SDK client for an admin resource."""
    root = (factory or AsyncClientFactory()).create(cfg).admin
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
    """Read SDK async iterator cursor if available."""
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
    """Collect dict/list page envelopes used by tests and some SDK methods."""
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
    """Invoke an admin SDK operation."""
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
    """Invoke a paginated admin operation."""

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
    """Run the Foundry Admin CLI."""
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
        AccessControlGuard(cfg, "ADMIN").check(resource, operation)
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

