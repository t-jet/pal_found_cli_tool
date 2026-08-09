#!/usr/bin/env python3
"""Foundry AIP Agents CLI with local alias lifecycle management."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from foundry_cli.common.access_control_guard import AccessControlGuard
from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.common.binary_download_handler import BinaryDownloadHandler
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
from foundry_cli.common.pagination_helper import PaginationHelper
from foundry_cli.common.retry import RetryHandler
from foundry_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status
from foundry_cli.common.session_manager import SessionManager, SessionState

logger = logging.getLogger(__name__)

OperationSpec = dict[str, Any]
_METADATA_ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "metadata-allow-list.md"
_PAGED = frozenset(
    {("agent", "all_sessions"), ("agent_version", "list"), ("session", "list")}
)
_ALIAS_BOUND = frozenset(
    {
        ("session", "blocking_continue"),
        ("session", "cancel"),
        ("session", "delete"),
        ("session", "get"),
        ("session", "rag_context"),
        ("session", "streaming_continue"),
        ("session", "update_title"),
        ("content", "get"),
        ("session_trace", "get"),
    }
)


class _ArgumentParser(argparse.ArgumentParser):
    """Raise parser failures into structured error handling."""

    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


class SDKContractError(Exception):
    """Raised when an SDK result violates its documented response contract."""

    exit_code = EXIT_SERVER_ERROR


def _op(
    resource: str,
    operation: str,
    client_path: str,
    method: str,
    positional: Iterable[str] = (),
    required: Iterable[str] = (),
    optional: Iterable[str] = (),
    *,
    json_objects: Iterable[str] = (),
    json_lists: Iterable[str] = (),
) -> OperationSpec:
    """Build one SDK operation specification."""
    return {
        "resource": resource,
        "operation": operation,
        "client_path": tuple(client_path.split(".")),
        "method": method,
        "positional": tuple(positional),
        "required": tuple(required),
        "optional": tuple(optional),
        "json_objects": frozenset(json_objects),
        "json_lists": frozenset(json_lists),
    }


OP_SPECS: tuple[OperationSpec, ...] = (
    _op("agent", "all_sessions", "Agent", "all_sessions"),
    _op("agent", "get", "Agent", "get", ("agent_rid",), optional=("version",)),
    _op(
        "agent_version",
        "get",
        "Agent.AgentVersion",
        "get",
        ("agent_rid", "agent_version_string"),
    ),
    _op("agent_version", "list", "Agent.AgentVersion", "list", ("agent_rid",)),
    _op(
        "session",
        "blocking_continue",
        "Agent.Session",
        "blocking_continue",
        required=("parameter_inputs", "user_input"),
        optional=("contexts_override", "session_trace_id"),
        json_objects=("parameter_inputs", "user_input"),
        json_lists=("contexts_override",),
    ),
    _op(
        "session",
        "cancel",
        "Agent.Session",
        "cancel",
        required=("message_id",),
        optional=("response",),
    ),
    _op("session", "create", "Agent.Session", "create", required=("alias", "agent_rid"), optional=("agent_version",)),
    _op("session", "delete", "Agent.Session", "delete"),
    _op("session", "get", "Agent.Session", "get"),
    _op("session", "list", "Agent.Session", "list", ("agent_rid",)),
    _op(
        "session",
        "rag_context",
        "Agent.Session",
        "rag_context",
        required=("parameter_inputs", "user_input"),
        json_objects=("parameter_inputs", "user_input"),
    ),
    _op(
        "session",
        "streaming_continue",
        "Agent.Session",
        "streaming_continue",
        required=("parameter_inputs", "user_input"),
        optional=("contexts_override", "message_id", "session_trace_id", "output_filename"),
        json_objects=("parameter_inputs", "user_input"),
        json_lists=("contexts_override",),
    ),
    _op("session", "update_title", "Agent.Session", "update_title", required=("title",)),
    _op("content", "get", "Agent.Session.Content", "get"),
    _op(
        "session_trace",
        "get",
        "Agent.Session.SessionTrace",
        "get",
        required=("session_trace_id",),
    ),
)

OPERATION_BY_RESOURCE: dict[str, dict[str, OperationSpec]] = {}
for _operation_spec in OP_SPECS:
    OPERATION_BY_RESOURCE.setdefault(_operation_spec["resource"], {})[
        _operation_spec["operation"]
    ] = _operation_spec


def _common_parser(*, paged: bool, timeout: bool = True) -> _ArgumentParser:
    """Create operation-level common options."""
    parser = _ArgumentParser(add_help=False)
    if timeout:
        parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--format", choices=("json", "toon", "auto"), default="auto")
    parser.add_argument("--pretty", action="store_true")
    if paged:
        parser.add_argument("--page-size", type=int, default=None)
        parser.add_argument("--page-token", default=None)
        parser.add_argument("--batch-pages", type=int, default=None)
    return parser


def _add_alias(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alias", required=True)


def _add_exchange_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--parameter-inputs-json", required=True, dest="parameter_inputs")
    parser.add_argument("--user-input-json", required=True, dest="user_input")


def build_parser() -> argparse.ArgumentParser:
    """Build parser for 15 SDK commands and local purge."""
    parser = _ArgumentParser(
        prog="foundry-aip-agents",
        description="Foundry AIP Agents CLI - 15 SDK operations plus local purge",
    )
    resources = parser.add_subparsers(dest="resource")

    agent = resources.add_parser("agent")
    agent_ops = agent.add_subparsers(dest="operation")
    agent_ops.add_parser("all-sessions", parents=[_common_parser(paged=True)])
    agent_get = agent_ops.add_parser("get", parents=[_common_parser(paged=False)])
    agent_get.add_argument("agent_rid")
    agent_get.add_argument("--version")

    versions = resources.add_parser("agent-version")
    version_ops = versions.add_subparsers(dest="operation")
    version_get = version_ops.add_parser("get", parents=[_common_parser(paged=False)])
    version_get.add_argument("agent_rid")
    version_get.add_argument("agent_version_string")
    version_list = version_ops.add_parser("list", parents=[_common_parser(paged=True)])
    version_list.add_argument("agent_rid")

    sessions = resources.add_parser("session")
    session_ops = sessions.add_subparsers(dest="operation")
    blocking = session_ops.add_parser("blocking-continue", parents=[_common_parser(paged=False)])
    _add_alias(blocking)
    _add_exchange_inputs(blocking)
    blocking.add_argument("--contexts-override-json", dest="contexts_override")
    blocking.add_argument("--session-trace-id")

    cancel = session_ops.add_parser("cancel", parents=[_common_parser(paged=False)])
    _add_alias(cancel)
    cancel.add_argument("--message-id", required=True)
    cancel.add_argument("--response")

    create = session_ops.add_parser("create", parents=[_common_parser(paged=False)])
    _add_alias(create)
    create.add_argument("--agent-rid", required=True)
    create.add_argument("--agent-version")

    for operation in ("delete", "get"):
        operation_parser = session_ops.add_parser(operation, parents=[_common_parser(paged=False)])
        _add_alias(operation_parser)

    session_list = session_ops.add_parser("list", parents=[_common_parser(paged=True)])
    session_list.add_argument("agent_rid")

    rag = session_ops.add_parser("rag-context", parents=[_common_parser(paged=False)])
    _add_alias(rag)
    _add_exchange_inputs(rag)

    streaming = session_ops.add_parser("streaming-continue", parents=[_common_parser(paged=False)])
    _add_alias(streaming)
    _add_exchange_inputs(streaming)
    streaming.add_argument("--contexts-override-json", dest="contexts_override")
    streaming.add_argument("--message-id")
    streaming.add_argument("--session-trace-id")
    streaming.add_argument("--output-filename")

    title = session_ops.add_parser("update-title", parents=[_common_parser(paged=False)])
    _add_alias(title)
    title.add_argument("--title", required=True)
    session_ops.add_parser("purge", parents=[_common_parser(paged=False, timeout=False)])

    content = resources.add_parser("content")
    content_ops = content.add_subparsers(dest="operation")
    content_get = content_ops.add_parser("get", parents=[_common_parser(paged=False)])
    _add_alias(content_get)

    traces = resources.add_parser("session-trace")
    trace_ops = traces.add_subparsers(dest="operation")
    trace_get = trace_ops.add_parser("get", parents=[_common_parser(paged=False)])
    _add_alias(trace_get)
    trace_get.add_argument("--session-trace-id", required=True)
    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return one catalog operation."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {resource}.{operation}") from exc


def _get_client(root_client: Any, client_path: tuple[str, ...]) -> Any:
    """Resolve exact public SDK nested resource path."""
    client = root_client.aip_agents
    for attribute in client_path:
        client = getattr(client, attribute)
    return client


def _parse_json_object(value: str, *, field: str) -> dict[str, Any]:
    """Decode a required JSON object."""
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must contain valid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{field} must be a JSON object")
    return result


def _parse_json_list(value: str, *, field: str) -> list[dict[str, Any]]:
    """Decode a JSON array containing only objects."""
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must contain valid JSON") from exc
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise ValueError(f"{field} must be a JSON array of objects")
    return result


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


def _required_text(value: Any, *, field: str) -> str:
    """Validate one non-empty scalar string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be empty")
    return value


def _validate_timeout(value: int) -> int:
    """Validate ADR-002 timeout range."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")
    return value


def _validate_inputs(spec: OperationSpec, args: argparse.Namespace) -> None:
    """Validate scalars and decode structured inputs before client creation."""
    for name in (*spec["positional"], *spec["required"]):
        value = getattr(args, name, None)
        if isinstance(value, str):
            _required_text(value, field=name)
    for name in spec["json_objects"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_object(value, field=name))
    for name in spec["json_lists"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_list(value, field=name))


async def _fetch_page(
    method: Any,
    *,
    page_size: int,
    page_token: str | None,
    request_timeout: int | None,
    call_args: tuple[Any, ...],
) -> dict[str, Any]:
    """Fetch exactly one decoded SDK response page."""
    raw_response = await method(
        *call_args,
        page_size=page_size,
        page_token=page_token,
        request_timeout=request_timeout,
    )
    page = raw_response.decode()
    return {
        "items": list(getattr(page, "data", None) or []),
        "next_page_token": getattr(page, "next_page_token", None),
    }


async def _paginate_operation(
    method: Any,
    args: argparse.Namespace,
    timeout: int | None,
    call_args: tuple[Any, ...],
) -> tuple[list[Any], PaginationHelper]:
    """Collect a bounded number of actual server pages."""
    helper = PaginationHelper(
        page_size=args.page_size,
        page_token=args.page_token,
        batch_pages=args.batch_pages,
    )

    async def fetch_page(**page_kwargs: Any) -> dict[str, Any]:
        return await _fetch_page(
            method,
            page_size=page_kwargs["page_size"],
            page_token=page_kwargs.get("page_token"),
            request_timeout=timeout,
            call_args=call_args,
        )

    return await helper.paginate(fetch_page), helper


async def _one_bytes_chunk(payload: bytes) -> AsyncIterator[bytes]:
    """Adapt an eager SDK byte result to download-handler input."""
    yield payload


def _load_alias(manager: SessionManager, alias: str) -> SessionState:
    """Load one canonical session alias."""
    return manager.load(alias)


def _record_session_use(
    manager: SessionManager,
    alias: str,
    state: SessionState,
    *,
    operation: str,
    succeeded: bool,
    completed: bool = False,
) -> None:
    """Persist sanitized use history without arguments or response data."""
    timestamp = datetime.now(UTC).isoformat()
    updated = SessionState(
        session_id=state.session_id,
        agent_rid=state.agent_rid,
        session_token=state.session_token,
        created_at=state.created_at,
        last_used_at=timestamp,
        status="completed" if completed else state.status,
        tool_history=[
            *state.tool_history,
            {"timestamp": timestamp, "operation": operation, "succeeded": succeeded},
        ],
    )
    manager.update(alias, updated)


def _sdk_call_parts(
    spec: OperationSpec,
    args: argparse.Namespace,
    state: SessionState | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Build documented SDK positional and keyword arguments."""
    positional = [getattr(args, name) for name in spec["positional"]]
    if state is not None:
        positional = [state.agent_rid, state.session_id]
        if spec["resource"] == "session_trace":
            positional.append(args.session_trace_id)
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        if name in {"alias", "agent_rid", "output_filename", "session_trace_id"} and (
            state is not None or name in {"alias", "output_filename"}
        ):
            continue
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = value
    if state is not None and "session_trace_id" in spec["optional"]:
        value = getattr(args, "session_trace_id", None)
        if value is not None:
            kwargs["session_trace_id"] = value
    return positional, kwargs


async def _invoke_sdk(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
    cfg: ConfigLoader,
    state: SessionState | None,
) -> tuple[Any, PaginationHelper | None]:
    """Invoke one complete SDK operation attempt."""
    positional, kwargs = _sdk_call_parts(spec, args, state)
    if (spec["resource"], spec["operation"]) in _PAGED:
        raw_method = getattr(client.with_raw_response, spec["method"])
        result, helper = await _paginate_operation(
            raw_method, args, timeout, tuple(positional)
        )
    else:
        kwargs["request_timeout"] = timeout
        result = await getattr(client, spec["method"])(*positional, **kwargs)
        helper = None
    if spec["operation"] == "streaming_continue":
        if not isinstance(result, bytes):
            raise SDKContractError("streaming_continue returned an invalid SDK result")
        download = await BinaryDownloadHandler(config=cfg).save(
            _one_bytes_chunk(result),
            original_filename=args.output_filename,
            namespace="aip_agents",
            operation="session.streaming_continue",
            content_length=None,
            content_encoding=None,
            mime_type=None,
        )
        result = download.to_dict()
    return result, helper


async def _create_session(
    client: Any,
    args: argparse.Namespace,
    timeout: int,
    manager: SessionManager,
) -> dict[str, Any]:
    """Create remote session and publish its local alias atomically."""
    retry = RetryHandler(timeout_s=timeout)

    async def create_remote() -> Any:
        create_kwargs: dict[str, Any] = {"request_timeout": timeout}
        if args.agent_version is not None:
            create_kwargs["agent_version"] = args.agent_version
        return await retry.execute(
            client.create,
            args.agent_rid,
            **create_kwargs,
        )

    async def delete_remote(session_id: str) -> None:
        await client.delete(
            args.agent_rid,
            session_id,
            request_timeout=timeout,
        )

    state = await manager.create(
        args.alias, args.agent_rid, create_remote, delete_remote
    )
    result = state.to_dict()
    result.pop("session_token", None)
    result["alias"] = SessionManager.normalize_alias(args.alias)
    return result


def _serialize_error(exception: BaseException) -> int:
    """Write one structured failure and return its ADR-001 exit code."""
    serializer = ErrorSerializer()
    status = sdk_http_status(exception)
    declared = getattr(exception, "exit_code", None)
    sdk_exit_code = sdk_exception_exit_code(exception)
    if isinstance(declared, int) and 1 <= declared <= 9:
        exit_code = declared
    elif status == 401:
        exit_code = EXIT_AUTH
    elif status == 403:
        exit_code = EXIT_PERMISSION_DENIED
    elif status == 404:
        exit_code = EXIT_NOT_FOUND
    elif status == 409:
        exit_code = EXIT_USER_INPUT
    elif status == 429:
        exit_code = EXIT_RATE_LIMIT
    elif sdk_exit_code is not None:
        exit_code = sdk_exit_code
    elif isinstance(exception, ConfigurationError):
        exit_code = EXIT_CONFIGURATION
    elif isinstance(exception, (TypeError, ValueError)):
        exit_code = EXIT_USER_INPUT
    elif isinstance(exception, PermissionError):
        exit_code = EXIT_PERMISSION_DENIED
    elif isinstance(exception, FileNotFoundError):
        exit_code = EXIT_NOT_FOUND
    elif isinstance(exception, TimeoutError):
        exit_code = EXIT_TIMEOUT
    else:
        exit_code = EXIT_SERVER_ERROR

    expose_message = isinstance(
        exception,
        (ConfigurationError, FileNotFoundError, PermissionError, TimeoutError, TypeError, ValueError),
    ) or declared in {
        EXIT_USER_INPUT,
        EXIT_NOT_FOUND,
        EXIT_ACCESS_CONTROL,
        EXIT_CONFIGURATION,
    }
    message = str(exception) if expose_message else "AIP Agents operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if isinstance(status, int):
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "AIP Agents operation failed",
        extra={
            "call_id": serializer.call_id,
            "exception_type": type(exception).__name__,
            "http_status": status,
        },
    )
    return exit_code


async def main() -> int:
    """Run one stateless Foundry AIP Agents invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise ValueError("an AIP Agents operation is required")
    except ValueError as exc:
        return _serialize_error(exc)

    try:
        cfg = ConfigLoader()
        cfg.load()
        LogSetup.configure(log_level=cfg.log_level)
        manager = SessionManager(config=cfg)
        manager.cleanup_expired()

        resource = args.resource.replace("-", "_")
        operation = args.operation.replace("-", "_")
        is_purge = (resource, operation) == ("session", "purge")
        spec = None if is_purge else _spec_for(resource, operation)
        if spec is not None:
            _validate_inputs(spec, args)

        guard = AccessControlGuard(
            cfg,
            "AIP_AGENTS",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        )
        guard.check(resource, operation)
        if is_purge:
            result: Any = {"purged_sessions": manager.purge()}
            print(OutputFormatter(format_setting="json", pretty=args.pretty).format(result))
            return EXIT_SUCCESS

        timeout = _validate_timeout(args.timeout if args.timeout is not None else cfg.timeout_s)
        state = (
            _load_alias(manager, args.alias)
            if (resource, operation) in _ALIAS_BOUND
            else None
        )
        factory = AsyncClientFactory()
        helper: PaginationHelper | None = None
        if spec is None:
            raise ValueError(f"Unknown operation: {resource}.{operation}")
        with factory.invocation_scope(cfg, include_attribution=False):
            root_client = factory.create(cfg, include_attribution=False)
            client = _get_client(root_client, spec["client_path"])
            if (resource, operation) == ("session", "create"):
                result = await _create_session(client, args, timeout, manager)
            else:
                retry = RetryHandler(timeout_s=timeout)
                try:
                    result, helper = await retry.execute(
                        _invoke_sdk, spec, client, args, timeout, cfg, state
                    )
                    if state is not None:
                        _record_session_use(
                            manager,
                            args.alias,
                            state,
                            operation=f"{resource}.{operation}",
                            succeeded=True,
                            completed=operation == "delete",
                        )
                except Exception:
                    if state is not None:
                        try:
                            _record_session_use(
                                manager,
                                args.alias,
                                state,
                                operation=f"{resource}.{operation}",
                                succeeded=False,
                            )
                        except Exception:
                            logger.warning(
                                "Could not persist failed session use",
                                extra={
                                    "session_alias": SessionManager.normalize_alias(
                                        args.alias
                                    )
                                },
                            )
                    raise

        force_json = operation == "streaming_continue"
        formatter = OutputFormatter(
            format_setting="json" if force_json else args.format,
            pretty=args.pretty,
        )
        print(formatter.format(_model_to_dict(result)))
        if helper is not None:
            helper.emit_metadata()
        return EXIT_SUCCESS
    except asyncio.CancelledError:
        return _serialize_error(TimeoutError("Operation cancelled"))
    except Exception as exc:
        return _serialize_error(exc)


def console_main() -> int:
    """Run async CLI through one event-loop boundary."""
    return asyncio.run(main())


if __name__ == "__main__":
    raise SystemExit(console_main())
