#!/usr/bin/env python3
"""Foundry Language Models CLI for messages and embeddings inference."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, NoReturn

from pal_found_cli.common.access_control_guard import AccessControlGuard
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
from pal_found_cli.common.retry import RetryHandler
from pal_found_cli.common.sdk_error_utils import sdk_exception_exit_code, sdk_http_status

logger = logging.getLogger(__name__)
OperationSpec = dict[str, Any]
_METADATA_ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "metadata-allow-list.md"


class _ArgumentParser(argparse.ArgumentParser):
    """Raise parser failures into the structured error boundary."""

    def error(self, message: str) -> NoReturn:
        raise CLIInputError("Invalid command input")


class CLIInputError(ValueError):
    """A locally generated input error whose message contains no supplied value."""

    exit_code = EXIT_USER_INPUT


OP_SPECS: tuple[OperationSpec, ...] = (
    {
        "resource": "anthropic_model",
        "operation": "messages",
        "client_path": ("AnthropicModel",),
        "method": "messages",
        "positional": ("model_id",),
        "required": ("max_tokens", "messages"),
        "optional": (
            "output_config",
            "stop_sequences",
            "system",
            "temperature",
            "thinking",
            "tool_choice",
            "tools",
            "top_k",
            "top_p",
        ),
        "object_json": frozenset({"output_config", "thinking", "tool_choice"}),
        "object_list_json": frozenset({"messages", "system", "tools"}),
        "string_list_json": frozenset({"stop_sequences"}),
    },
    {
        "resource": "open_ai_model",
        "operation": "embeddings",
        "client_path": ("OpenAiModel",),
        "method": "embeddings",
        "positional": ("model_id",),
        "required": ("input",),
        "optional": ("dimensions", "encoding_format"),
        "object_json": frozenset(),
        "object_list_json": frozenset(),
        "string_list_json": frozenset({"input"}),
    },
)

OPERATION_BY_RESOURCE = {
    spec["resource"]: {spec["operation"]: spec} for spec in OP_SPECS
}


def _common_parser() -> _ArgumentParser:
    """Create shared operation options."""
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--format", choices=("json", "toon", "auto"), default="auto")
    parser.add_argument("--pretty", action="store_true")
    return parser


def build_parser() -> argparse.ArgumentParser:
    """Build the two-operation Language Models parser."""
    parser = _ArgumentParser(
        prog="pal-found-language-models",
        description="Foundry Language Models CLI - messages and embeddings",
    )
    resources = parser.add_subparsers(dest="resource")
    anthropic = resources.add_parser("anthropic-model")
    anthropic_ops = anthropic.add_subparsers(dest="operation")
    messages = anthropic_ops.add_parser("messages", parents=[_common_parser()])
    messages.add_argument("model_id")
    messages.add_argument("--max-tokens", required=True, type=int)
    messages.add_argument("--messages-json", required=True, dest="messages")
    messages.add_argument("--output-config-json", dest="output_config")
    messages.add_argument("--stop-sequences-json", dest="stop_sequences")
    messages.add_argument("--system-json", dest="system")
    messages.add_argument("--temperature", type=float)
    messages.add_argument("--thinking-json", dest="thinking")
    messages.add_argument("--tool-choice-json", dest="tool_choice")
    messages.add_argument("--tools-json", dest="tools")
    messages.add_argument("--top-k", type=int)
    messages.add_argument("--top-p", type=float)

    open_ai = resources.add_parser("open-ai-model")
    open_ai_ops = open_ai.add_subparsers(dest="operation")
    embeddings = open_ai_ops.add_parser("embeddings", parents=[_common_parser()])
    embeddings.add_argument("model_id")
    embeddings.add_argument("--input-json", required=True, dest="input")
    embeddings.add_argument("--dimensions", type=int)
    embeddings.add_argument("--encoding-format", choices=("FLOAT", "BASE64"))
    return parser


def _spec_for(resource: str, operation: str) -> OperationSpec:
    """Return one catalog specification."""
    try:
        return OPERATION_BY_RESOURCE[resource][operation]
    except KeyError as exc:
        raise CLIInputError(f"Unknown operation: {resource}.{operation}") from exc


def _required_text(value: Any, *, field: str) -> str:
    """Validate a non-empty string while preserving its original value."""
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


def _parse_json_object_list(value: str, *, field: str) -> list[dict[str, Any]]:
    """Decode one JSON array containing only objects."""
    result = _decode_json(value, field=field)
    if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
        raise CLIInputError(f"{field} must be a JSON array of objects")
    return result


def _parse_json_string_list(value: str, *, field: str) -> list[str]:
    """Decode one JSON array containing only strings."""
    result = _decode_json(value, field=field)
    if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
        raise CLIInputError(f"{field} must be a JSON array of strings")
    return result


def _validate_inputs(spec: OperationSpec, args: argparse.Namespace) -> None:
    """Validate scalar and outer JSON shapes before client creation."""
    _required_text(args.model_id, field="model_id")
    for name in spec["object_json"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_object(value, field=name))
    for name in spec["object_list_json"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_object_list(value, field=name))
    for name in spec["string_list_json"]:
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, _parse_json_string_list(value, field=name))


def _validate_timeout(value: int) -> int:
    """Validate the effective ADR-002 timeout."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
        raise CLIInputError("timeout must be between 1 and 3600 seconds")
    return value


def _get_client(root_client: Any, client_path: tuple[str, ...]) -> Any:
    """Resolve one exact public Language Models SDK resource."""
    client = root_client.language_models
    for attribute in client_path:
        client = getattr(client, attribute)
    return client


def _model_to_dict(value: Any) -> Any:
    """Convert SDK models to JSON-compatible values."""
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


async def _invoke_sdk(
    spec: OperationSpec,
    client: Any,
    args: argparse.Namespace,
    timeout: int,
) -> Any:
    """Call one decoded SDK inference method with exact documented arguments."""
    kwargs: dict[str, Any] = {}
    for name in (*spec["required"], *spec["optional"]):
        value = getattr(args, name, None)
        if value is not None:
            kwargs[name] = value
    kwargs["request_timeout"] = timeout
    return await getattr(client, spec["method"])(args.model_id, **kwargs)


def _serialize_error(exception: BaseException) -> int:
    """Write one privacy-safe ADR-001 error envelope."""
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
    elif status == 409:
        exit_code = EXIT_USER_INPUT
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
    safe = isinstance(exception, (CLIInputError, ConfigurationError, TimeoutError)) or declared in {
        EXIT_ACCESS_CONTROL,
        EXIT_CONFIGURATION,
    }
    message = str(exception) if safe else "Language Models operation failed"
    envelope = serializer.create_error_envelope(
        exit_code, message, type(exception).__name__, serializer.call_id
    )
    if status is not None:
        envelope["http_status"] = status
    sys.stdout.write(json.dumps(envelope, default=str) + "\n")
    sys.stdout.flush()
    logger.error(
        "Language Models operation failed",
        extra={"call_id": serializer.call_id, "exception_type": type(exception).__name__, "http_status": status},
    )
    return exit_code


async def main() -> int:
    """Run one stateless Language Models invocation."""
    parser = build_parser()
    try:
        args = parser.parse_args()
        if not args.resource or not getattr(args, "operation", None):
            raise CLIInputError("a Language Models operation is required")
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
        timeout = _validate_timeout(args.timeout if args.timeout is not None else cfg.timeout_s)
        AccessControlGuard(
            cfg,
            "LANGUAGE_MODELS",
            metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
        ).check(resource, operation)

        factory = AsyncClientFactory()
        with factory.invocation_scope(cfg, include_attribution=True):
            root_client = factory.create(cfg, include_attribution=True)
            client = _get_client(root_client, spec["client_path"])
            result = await RetryHandler(timeout_s=timeout).execute(
                _invoke_sdk, spec, client, args, timeout
            )
            rendered = OutputFormatter(
                format_setting=args.format, pretty=args.pretty
            ).format(_model_to_dict(result))
        print(rendered)
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
