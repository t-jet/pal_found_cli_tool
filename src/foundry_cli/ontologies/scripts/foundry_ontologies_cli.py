#!/usr/bin/env python3
# ruff: noqa: E402
"""Foundry Ontologies CLI - 67 canonical API v2 operations."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import sys
from collections.abc import AsyncIterable, Iterable
from pathlib import Path
from typing import Any, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT: Optional[str] = None
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

from foundry_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.common.binary_download_handler import BinaryDownloadHandler
from foundry_cli.common.config_loader import ConfigLoader
from foundry_cli.common.error_serializer import (
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
from foundry_cli.common.log_setup import LogSetup
from foundry_cli.common.output_formatter import OutputFormatter
from foundry_cli.common.pagination_helper import PaginationHelper
from foundry_cli.common.retry import RetryHandler

logger = logging.getLogger(__name__)


OperationSpec = dict[str, Any]


def _op(
    resource: str,
    operation: str,
    client_path: str,
    method: str,
    positional: Iterable[str] = (),
    optional: Iterable[str] = (),
    *,
    json_args: Iterable[str] = (),
    binary_download: bool = False,
    binary_upload: bool = False,
) -> OperationSpec:
    return {
        "resource": resource,
        "operation": operation,
        "client_path": client_path,
        "method": method,
        "positional": tuple(positional),
        "optional": tuple(optional),
        "json_args": frozenset(json_args),
        "binary_download": binary_download,
        "binary_upload": binary_upload,
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op("action", "apply", "Action", "apply", ("ontology", "action"), ("parameters", "branch", "options", "sdk_package_rid", "sdk_version", "transaction_id"), json_args=("parameters", "options")),
    _op("action", "apply_batch", "Action", "apply_batch", ("ontology", "action"), ("requests", "branch", "options", "sdk_package_rid", "sdk_version"), json_args=("requests", "options")),
    _op("action", "apply_with_overrides", "Action", "apply_with_overrides", ("ontology", "action"), ("overrides", "request", "branch", "sdk_package_rid", "sdk_version", "transaction_id"), json_args=("overrides", "request")),
    _op("action_type", "get", "Ontology.ActionType", "get", ("ontology", "action_type"), ("branch",)),
    _op("action_type", "get_by_rid", "Ontology.ActionType", "get_by_rid", ("ontology", "action_type_rid"), ("branch",)),
    _op("action_type", "get_by_rid_batch", "Ontology.ActionType", "get_by_rid_batch", ("ontology",), ("requests", "branch"), json_args=("requests",)),
    _op("action_type", "list", "Ontology.ActionType", "list", ("ontology",), ("branch", "page_size", "page_token")),
    _op("action_type_full_metadata", "get", "ActionTypeFullMetadata", "get", ("ontology", "action_type"), ("branch",)),
    _op("action_type_full_metadata", "list", "ActionTypeFullMetadata", "list", ("ontology",), ("branch", "object_type_api_names", "page_size", "page_token"), json_args=("object_type_api_names",)),
    _op("attachment", "get", "Attachment", "get", ("attachment_rid",)),
    _op("attachment", "read", "Attachment", "read", ("attachment_rid",), binary_download=True),
    _op("attachment", "upload", "Attachment", "upload", (), ("content_length", "content_type", "filename"), binary_upload=True),
    _op("attachment", "upload_with_rid", "Attachment", "upload_with_rid", ("attachment_rid",), ("content_length", "content_type", "filename", "preview"), binary_upload=True),
    _op("attachment_property", "get_attachment", "AttachmentProperty", "get_attachment", ("ontology", "object_type", "primary_key", "property"), ("sdk_package_rid", "sdk_version")),
    _op("attachment_property", "get_attachment_by_rid", "AttachmentProperty", "get_attachment_by_rid", ("ontology", "object_type", "primary_key", "property", "attachment_rid"), ("sdk_package_rid", "sdk_version")),
    _op("attachment_property", "read_attachment", "AttachmentProperty", "read_attachment", ("ontology", "object_type", "primary_key", "property"), ("sdk_package_rid", "sdk_version"), binary_download=True),
    _op("attachment_property", "read_attachment_by_rid", "AttachmentProperty", "read_attachment_by_rid", ("ontology", "object_type", "primary_key", "property", "attachment_rid"), ("sdk_package_rid", "sdk_version"), binary_download=True),
    _op("cipher_text_property", "decrypt", "CipherTextProperty", "decrypt", ("ontology", "object_type", "primary_key", "property")),
    _op("geotemporal_series_property", "get_geotemporal_series_latest_value", "GeotemporalSeriesProperty", "get_geotemporal_series_latest_value", ("ontology", "object_type", "primary_key", "property_name"), ("sdk_package_rid", "sdk_version")),
    _op("geotemporal_series_property", "stream_geotemporal_series_historic_values", "GeotemporalSeriesProperty", "stream_geotemporal_series_historic_values", ("ontology", "object_type", "primary_key", "property_name"), ("range", "sdk_package_rid", "sdk_version"), json_args=("range",), binary_download=True),
    _op("linked_object", "get_linked_object", "LinkedObject", "get_linked_object", ("ontology", "object_type", "primary_key", "link_type", "linked_object_primary_key"), ("branch", "exclude_rid", "sdk_package_rid", "sdk_version", "select"), json_args=("select",)),
    _op("linked_object", "list_linked_objects", "LinkedObject", "list_linked_objects", ("ontology", "object_type", "primary_key", "link_type"), ("branch", "exclude_rid", "order_by", "page_size", "page_token", "sdk_package_rid", "sdk_version", "select", "snapshot"), json_args=("order_by", "select")),
    _op("media_reference_property", "get_media_content", "MediaReferenceProperty", "get_media_content", ("ontology", "object_type", "primary_key", "property"), ("preview", "sdk_package_rid", "sdk_version"), binary_download=True),
    _op("media_reference_property", "get_media_metadata", "MediaReferenceProperty", "get_media_metadata", ("ontology", "object_type", "primary_key", "property"), ("preview", "sdk_package_rid", "sdk_version")),
    _op("media_reference_property", "upload", "MediaReferenceProperty", "upload", ("ontology", "object_type", "property"), ("media_item_path", "preview"), binary_upload=True),
    _op("object_type", "get", "Ontology.ObjectType", "get", ("ontology", "object_type"), ("branch",)),
    _op("object_type", "get_by_rid_batch", "Ontology.ObjectType", "get_by_rid_batch", ("ontology",), ("requests", "branch"), json_args=("requests",)),
    _op("object_type", "get_edits_history", "Ontology.ObjectType", "get_edits_history", ("ontology", "object_type"), ("branch", "filters", "include_all_previous_properties", "object_primary_key", "page_size", "page_token", "sort_order"), json_args=("filters",)),
    _op("object_type", "get_full_metadata", "Ontology.ObjectType", "get_full_metadata", ("ontology", "object_type"), ("branch", "preview", "sdk_package_rid", "sdk_version")),
    _op("object_type", "get_outgoing_link_type", "Ontology.ObjectType", "get_outgoing_link_type", ("ontology", "object_type", "link_type"), ("branch",)),
    _op("object_type", "list", "Ontology.ObjectType", "list", ("ontology",), ("branch", "page_size", "page_token")),
    _op("object_type", "list_outgoing_link_types", "Ontology.ObjectType", "list_outgoing_link_types", ("ontology", "object_type"), ("branch", "page_size", "page_token")),
    _op("ontology", "get", "Ontology", "get", ("ontology",)),
    _op("ontology", "get_full_metadata", "Ontology", "get_full_metadata", ("ontology",), ("branch",)),
    _op("ontology", "list", "Ontology", "list"),
    _op("ontology", "load_metadata", "Ontology", "load_metadata", ("ontology",), ("action_types", "interface_types", "link_types", "object_types", "query_types", "branch", "preview"), json_args=("action_types", "interface_types", "link_types", "object_types", "query_types")),
    _op("ontology_interface", "aggregate", "OntologyInterface", "aggregate", ("ontology", "interface_type"), ("aggregation", "group_by", "accuracy", "branch", "preview", "where"), json_args=("aggregation", "group_by", "where")),
    _op("ontology_interface", "get", "OntologyInterface", "get", ("ontology", "interface_type"), ("branch", "preview", "sdk_package_rid", "sdk_version")),
    _op("ontology_interface", "get_outgoing_interface_link_type", "OntologyInterface", "get_outgoing_interface_link_type", ("ontology", "interface_type", "interface_link_type"), ("branch",)),
    _op("ontology_interface", "list", "OntologyInterface", "list", ("ontology",), ("branch", "page_size", "page_token", "preview")),
    _op("ontology_interface", "list_interface_linked_objects", "OntologyInterface", "list_interface_linked_objects", ("ontology", "interface_type", "object_type", "primary_key", "interface_link_type"), ("branch", "exclude_rid", "order_by", "page_size", "page_token", "preview", "select", "snapshot"), json_args=("order_by", "select")),
    _op("ontology_interface", "list_objects_for_interface", "OntologyInterface", "list_objects_for_interface", ("ontology", "interface_type"), ("branch", "exclude_rid", "order_by", "page_size", "page_token", "preview", "select", "snapshot"), json_args=("order_by", "select")),
    _op("ontology_interface", "list_outgoing_interface_link_types", "OntologyInterface", "list_outgoing_interface_link_types", ("ontology", "interface_type"), ("branch",)),
    _op("ontology_interface", "search", "OntologyInterface", "search", ("ontology", "interface_type"), ("augmented_interface_property_types", "augmented_properties", "augmented_shared_property_types", "other_interface_types", "selected_interface_property_types", "selected_object_types", "selected_shared_property_types", "branch", "order_by", "page_size", "page_token", "preview", "where"), json_args=("augmented_interface_property_types", "augmented_properties", "augmented_shared_property_types", "other_interface_types", "selected_interface_property_types", "selected_object_types", "selected_shared_property_types", "order_by", "where")),
    _op("ontology_object", "aggregate", "OntologyObject", "aggregate", ("ontology", "object_type"), ("aggregation", "group_by", "accuracy", "branch", "sdk_package_rid", "sdk_version", "where"), json_args=("aggregation", "group_by", "where")),
    _op("ontology_object", "count", "OntologyObject", "count", ("ontology", "object_type"), ("branch", "sdk_package_rid", "sdk_version")),
    _op("ontology_object", "get", "OntologyObject", "get", ("ontology", "object_type", "primary_key"), ("branch", "exclude_rid", "sdk_package_rid", "sdk_version", "select"), json_args=("select",)),
    _op("ontology_object", "list", "OntologyObject", "list", ("ontology", "object_type"), ("branch", "exclude_rid", "order_by", "page_size", "page_token", "sdk_package_rid", "sdk_version", "select", "snapshot"), json_args=("order_by", "select")),
    _op("ontology_object", "search", "OntologyObject", "search", ("ontology", "object_type"), ("select", "branch", "exclude_rid", "order_by", "page_size", "page_token", "sdk_package_rid", "sdk_version", "select_v2", "snapshot", "where"), json_args=("select", "order_by", "select_v2", "where")),
    _op("ontology_object_set", "aggregate", "OntologyObjectSet", "aggregate", ("ontology",), ("aggregation", "group_by", "object_set", "accuracy", "branch", "include_compute_usage", "sdk_package_rid", "sdk_version", "transaction_id"), json_args=("aggregation", "group_by", "object_set")),
    _op("ontology_object_set", "create_temporary", "OntologyObjectSet", "create_temporary", ("ontology",), ("object_set", "branch", "sdk_package_rid", "sdk_version"), json_args=("object_set",)),
    _op("ontology_object_set", "get", "OntologyObjectSet", "get", ("ontology", "object_set_rid")),
    _op("ontology_object_set", "load", "OntologyObjectSet", "load", ("ontology",), ("object_set", "select", "branch", "exclude_rid", "include_compute_usage", "load_property_securities", "order_by", "page_size", "page_token", "sdk_package_rid", "sdk_version", "select_v2", "snapshot", "transaction_id"), json_args=("object_set", "select", "order_by", "select_v2")),
    _op("ontology_object_set", "load_links", "OntologyObjectSet", "load_links", ("ontology",), ("links", "object_set", "branch", "include_compute_usage", "page_token", "preview", "sdk_package_rid", "sdk_version"), json_args=("links", "object_set")),
    _op("ontology_object_set", "load_multiple_object_types", "OntologyObjectSet", "load_multiple_object_types", ("ontology",), ("object_set", "select", "branch", "exclude_rid", "include_compute_usage", "load_property_securities", "order_by", "page_size", "page_token", "preview", "sdk_package_rid", "sdk_version", "select_v2", "snapshot", "transaction_id"), json_args=("object_set", "select", "order_by", "select_v2")),
    _op("ontology_object_set", "load_objects_or_interfaces", "OntologyObjectSet", "load_objects_or_interfaces", ("ontology",), ("object_set", "select", "branch", "exclude_rid", "order_by", "page_size", "page_token", "preview", "sdk_package_rid", "sdk_version", "select_v2", "snapshot", "transaction_id"), json_args=("object_set", "select", "order_by", "select_v2")),
    _op("ontology_transaction", "post_edits", "OntologyTransaction", "post_edits", ("ontology", "transaction_id"), ("edits", "preview", "sdk_package_rid", "sdk_version"), json_args=("edits",)),
    _op("ontology_value_type", "get", "OntologyValueType", "get", ("ontology", "value_type"), ("preview",)),
    _op("ontology_value_type", "list", "OntologyValueType", "list", ("ontology",), ("preview",)),
    _op("query", "execute", "Query", "execute", ("ontology", "query_api_name"), ("parameters", "attribution", "branch", "sdk_package_rid", "sdk_version", "transaction_id", "version"), json_args=("parameters", "attribution")),
    _op("query_type", "get", "Ontology.QueryType", "get", ("ontology", "query_api_name"), ("sdk_package_rid", "sdk_version", "version")),
    _op("query_type", "list", "Ontology.QueryType", "list", ("ontology",), ("page_size", "page_token")),
    _op("time_series_property_v2", "get_first_point", "TimeSeriesPropertyV2", "get_first_point", ("ontology", "object_type", "primary_key", "property"), ("sdk_package_rid", "sdk_version")),
    _op("time_series_property_v2", "get_last_point", "TimeSeriesPropertyV2", "get_last_point", ("ontology", "object_type", "primary_key", "property"), ("sdk_package_rid", "sdk_version")),
    _op("time_series_property_v2", "stream_points", "TimeSeriesPropertyV2", "stream_points", ("ontology", "object_type", "primary_key", "property"), ("aggregate", "format", "range", "sdk_package_rid", "sdk_version"), json_args=("aggregate", "range"), binary_download=True),
    _op("time_series_value_bank_property", "get_latest_value", "TimeSeriesValueBankProperty", "get_latest_value", ("ontology", "object_type", "primary_key", "property_name"), ("sdk_package_rid", "sdk_version")),
    _op("time_series_value_bank_property", "stream_values", "TimeSeriesValueBankProperty", "stream_values", ("ontology", "object_type", "primary_key", "property"), ("range", "sdk_package_rid", "sdk_version"), json_args=("range",), binary_download=True),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_spec["resource"], {})[_spec["operation"]] = _spec


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
    return name.replace("_", "-")


def _common_parser() -> argparse.ArgumentParser:
    """Build shared parser for global options."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=None)
    common.add_argument("--format", choices=["json", "toon", "auto"], default="auto")
    common.add_argument("--pretty", action="store_true")
    common.add_argument("--page-size", type=int, default=None, dest="page_size")
    common.add_argument("--page-token", type=str, default=None, dest="page_token")
    common.add_argument("--batch-pages", type=int, default=None, dest="batch_pages")
    common.add_argument("--output-filename", default=None, dest="output_filename")
    common.add_argument("--content-length", type=int, default=None, dest="content_length")
    common.add_argument("--content-type", default=None, dest="content_type")
    common.add_argument("--body-file", default=None, dest="body_file")
    return common


def build_parser() -> argparse.ArgumentParser:
    """Build argparse parser with all 67 ontology operations."""
    parser = argparse.ArgumentParser(
        prog="foundry_ontologies_cli",
        description="Foundry Ontologies CLI - 67 operations",
    )
    subparsers = parser.add_subparsers(dest="resource", help="Resource type")

    for resource in sorted(OPERATION_BY_RESOURCE):
        res_parser = subparsers.add_parser(_kebab(resource))
        op_sub = res_parser.add_subparsers(dest="operation")
        for operation, spec in sorted(OPERATION_BY_RESOURCE[resource].items()):
            op_parser = op_sub.add_parser(_kebab(operation), parents=[_common_parser()])
            for arg_name in spec["positional"]:
                op_parser.add_argument(arg_name)
            for arg_name in spec["optional"]:
                if arg_name in {"page_size", "page_token", "content_length", "content_type"}:
                    continue
                if arg_name == "format":
                    op_parser.add_argument("--stream-format", default=None, dest="stream_format")
                    continue
                flag = "--" + _kebab(arg_name)
                if arg_name.startswith("include_") or arg_name in {
                    "preview",
                    "snapshot",
                    "exclude_rid",
                    "load_property_securities",
                    "include_all_previous_properties",
                }:
                    op_parser.add_argument(flag, action="store_true", default=None, dest=arg_name)
                else:
                    op_parser.add_argument(flag, default=None, dest=arg_name)

    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(cfg: ConfigLoader, resource: str, factory: AsyncClientFactory | None = None) -> Any:
    """Get SDK client for an ontology resource."""
    root = (factory or AsyncClientFactory()).create(cfg).ontologies
    spec = next(iter(OPERATION_BY_RESOURCE[resource].values()))
    client = root
    for attr in spec["client_path"].split("."):
        client = getattr(client, attr)
    return client


def _coerce_arg(value: Any, *, json_arg: bool) -> Any:
    if value is None:
        return None
    if json_arg and isinstance(value, str):
        return json.loads(value)
    return value


def _read_binary_body(args: argparse.Namespace) -> bytes:
    body_file = getattr(args, "body_file", None)
    if not body_file:
        raise ValueError("body_file is required for binary upload operations")
    return Path(body_file).read_bytes()


async def _bytes_iter(value: bytes | bytearray | AsyncIterable[bytes]) -> AsyncIterable[bytes]:
    if isinstance(value, (bytes, bytearray)):
        yield bytes(value)
        return
    if isinstance(value, Iterable):
        for chunk in value:
            yield bytes(chunk)
        return
    async for chunk in value:
        yield chunk


async def _resolve_result(value: Any) -> Any:
    """Await SDK calls that return awaitables; pass iterators through unchanged."""
    if asyncio.isfuture(value) or inspect.isawaitable(value):
        return await value
    return value


async def _persist_binary_result(
    result: Any,
    *,
    cfg: ConfigLoader,
    resource: str,
    operation: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    handler = BinaryDownloadHandler(config=cfg)
    download = await handler.save(
        _bytes_iter(result),
        original_filename=getattr(args, "output_filename", None),
        namespace="ontologies",
        operation=f"{resource}.{operation}",
        content_length=None,
        content_encoding=None,
        mime_type=None,
    )
    return download.to_dict()


async def _invoke(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: Optional[int],
    cfg: ConfigLoader | None = None,
) -> Any:
    """Invoke an ontology SDK operation."""
    spec = _spec_for(resource, operation)
    method = getattr(client, spec["method"])
    positional = [getattr(args, name) for name in spec["positional"]]
    kwargs: dict[str, Any] = {}

    for name in spec["optional"]:
        if name in {"content_length", "content_type"}:
            continue
        if name == "format":
            value = getattr(args, "stream_format", None)
            if value is None and getattr(args, "format", None) not in {"json", "toon", "auto"}:
                value = getattr(args, "format", None)
        else:
            value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = _coerce_arg(value, json_arg=name in spec["json_args"])

    if spec["binary_upload"]:
        body = _read_binary_body(args)
        body_index = len(positional)
        if spec["resource"] in {"attachment", "media_reference_property"}:
            positional.insert(body_index, body)
        if spec["resource"] == "attachment":
            if not getattr(args, "filename", None):
                raise ValueError("filename is required for attachment upload operations")
            kwargs["content_length"] = getattr(args, "content_length", None) or len(body)
            kwargs["content_type"] = getattr(args, "content_type", None) or "application/octet-stream"

    kwargs["request_timeout"] = timeout
    result = await _resolve_result(method(*positional, **kwargs))

    if spec["binary_download"]:
        if cfg is None:
            cfg = ConfigLoader()
        return await _persist_binary_result(
            result,
            cfg=cfg,
            resource=resource,
            operation=operation,
            args=args,
        )
    return result


PAGINATED_OPS = frozenset(
    (spec["resource"], spec["operation"])
    for spec in OP_SPECS
    if "page_size" in spec["optional"] and "page_token" in spec["optional"]
)


def _is_paginated(resource: str, operation: str) -> bool:
    """Return true if operation supports SDK pagination args."""
    return (resource, operation) in PAGINATED_OPS


async def _invoke_paginated(
    resource: str,
    operation: str,
    client: Any,
    args: argparse.Namespace,
    timeout: Optional[int],
    helper: PaginationHelper,
    cfg: ConfigLoader,
) -> Any:
    """Invoke a paginated ontology operation through PaginationHelper."""

    async def _single_page(**page_kwargs: Any) -> Any:
        paged_args = argparse.Namespace(**vars(args))
        paged_args.page_size = page_kwargs.get("page_size", getattr(args, "page_size", None))
        paged_args.page_token = page_kwargs.get("page_token", None)
        return await _invoke(resource, operation, client, paged_args, timeout, cfg)

    return await helper.paginate(_single_page)


async def main() -> int:
    """Run the Foundry Ontologies CLI."""
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
    logger.info("Executing operation", extra={"resource": resource, "operation": operation})

    try:
        _spec_for(resource, operation)
    except ValueError as exc:
        ErrorSerializer().serialize(exc)
        return EXIT_USER_INPUT

    try:
        AccessControlGuard(cfg, "ONTOLOGIES").check(resource, operation)
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
                    cfg,
                )
            else:
                result = await retry_handler.execute(
                    _invoke,
                    resource,
                    operation,
                    client,
                    args,
                    timeout,
                    cfg,
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
        ErrorSerializer().serialize(exc)
        return EXIT_SERVER_ERROR


def console_main() -> int:
    """Run the async CLI from the console script entry point."""
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(console_main())
