"""Tests for the two-operation Foundry Language Models CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from pal_found_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from pal_found_cli.common import async_client_factory as factory_module
from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.language_models.scripts import pal_found_language_models_cli as cli
from foundry_sdk import ATTRIBUTION_VAR
from foundry_sdk._errors import BadRequestError, ServiceUnavailable, UnauthorizedError
from foundry_sdk import AsyncFoundryClient, UserTokenAuth


class _Scope(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> Literal[False]:
        return False


class _Factory:
    def __init__(self, root: Any) -> None:
        self.root = root
        self.create_calls = 0
        self.scope_kwargs: dict[str, Any] = {}
        self.create_kwargs: dict[str, Any] = {}

    def invocation_scope(self, cfg: Any, **kwargs: Any) -> _Scope:
        self.scope_kwargs = kwargs
        return _Scope()

    def create(self, cfg: Any, **kwargs: Any) -> Any:
        self.create_calls += 1
        self.create_kwargs = kwargs
        return self.root


class _ImmediateRetry:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def execute(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await function(*args, **kwargs)


class _Cfg:
    log_level = "ERROR"
    timeout_s = 30
    global_readonly = False
    global_metadata_only = False

    def load(self) -> None:
        return None


def _root() -> tuple[Any, Any, Any]:
    anthropic = SimpleNamespace(messages=AsyncMock())
    open_ai = SimpleNamespace(embeddings=AsyncMock())
    root = SimpleNamespace(
        language_models=SimpleNamespace(
            AnthropicModel=anthropic,
            OpenAiModel=open_ai,
        )
    )
    return root, anthropic, open_ai


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


def test_catalog_contains_exact_two_nested_operations() -> None:
    assert [
        (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        for spec in cli.OP_SPECS
    ] == [
        ("anthropic_model", "messages", ("AnthropicModel",), "messages"),
        ("open_ai_model", "embeddings", ("OpenAiModel",), "embeddings"),
    ]


def test_parser_exposes_only_approved_surface() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(
        ["anthropic-model", "messages", "model", "--max-tokens", "10", "--messages-json", "[]"]
    ).operation == "messages"
    assert parser.parse_args(
        ["open-ai-model", "embeddings", "model", "--input-json", '["x"]']
    ).operation == "embeddings"
    with pytest.raises(ValueError):
        parser.parse_args(["open-ai-model", "list"])
    help_text = parser.format_help()
    for excluded in ("page-size", "output-filename", "alias", "streaming"):
        assert excluded not in help_text


def test_json_validators_enforce_outer_shapes_without_echo() -> None:
    assert cli._parse_json_object("{}", field="thinking") == {}
    assert cli._parse_json_object_list("[{}]", field="messages") == [{}]
    assert cli._parse_json_string_list('["x"]', field="input") == ["x"]
    sentinel = "private-prompt"
    with pytest.raises(ValueError) as captured:
        cli._parse_json_string_list(json.dumps([sentinel, 1]), field="input")
    assert sentinel not in str(captured.value)
    with pytest.raises(ValueError, match="valid JSON"):
        cli._parse_json_object("{", field="thinking")


@pytest.mark.asyncio
async def test_messages_required_only_exact_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, anthropic, _ = _root()
    anthropic.messages.return_value = SimpleNamespace(to_dict=lambda: {"id": "answer"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "anthropic-model", "messages", " original-model ", "--max-tokens", "20", "--messages-json", '[{"role":"USER"}]', "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"id": "answer"}
    anthropic.messages.assert_awaited_once_with(
        " original-model ",
        max_tokens=20,
        messages=[{"role": "USER"}],
        request_timeout=30,
    )
    assert factory.scope_kwargs == {"include_attribution": True}
    assert factory.create_kwargs == {"include_attribution": True}


@pytest.mark.asyncio
async def test_messages_all_options_forward_exact_names() -> None:
    client = SimpleNamespace(messages=AsyncMock(return_value={"ok": True}))
    spec = cli._spec_for("anthropic_model", "messages")
    args = argparse.Namespace(
        model_id="model",
        max_tokens=1,
        messages=[{}],
        output_config={"x": 1},
        stop_sequences=["stop"],
        system=[{}],
        temperature=0.5,
        thinking={"type": "disabled"},
        tool_choice={"type": "auto"},
        tools=[{}],
        top_k=2,
        top_p=0.9,
    )
    await cli._invoke_sdk(spec, client, args, 12)
    kwargs = client.messages.await_args.kwargs
    assert set(kwargs) == {
        "max_tokens", "messages", "output_config", "stop_sequences", "system",
        "temperature", "thinking", "tool_choice", "tools", "top_k", "top_p",
        "request_timeout",
    }
    assert "attribution" not in kwargs and "preview" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", [None, "FLOAT", "BASE64"])
async def test_embeddings_dispatch_omits_absent_optionals(encoding: str | None) -> None:
    client = SimpleNamespace(embeddings=AsyncMock(return_value={"data": []}))
    args = argparse.Namespace(
        model_id="embedding-model",
        input=["one", "two"],
        dimensions=None,
        encoding_format=encoding,
    )
    await cli._invoke_sdk(cli._spec_for("open_ai_model", "embeddings"), client, args, 8)
    expected: dict[str, Any] = {"input": ["one", "two"], "request_timeout": 8}
    if encoding is not None:
        expected["encoding_format"] = encoding
    client.embeddings.assert_awaited_once_with("embedding-model", **expected)


@pytest.mark.asyncio
async def test_invalid_json_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, *_ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    sentinel = "secret-input"
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "open-ai-model", "embeddings", "model", "--input-json", json.dumps([sentinel, 1])],
    )
    assert await cli.main() == 1
    output = capsys.readouterr().out
    assert sentinel not in output
    assert factory.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["cmd", "anthropic-model", "messages", "model", "--max-tokens", "1", "--messages-json", "[]"],
        ["cmd", "open-ai-model", "embeddings", "model", "--input-json", "[]"],
    ],
)
async def test_global_disable_blocks_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    root, *_ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLED", "false")
    monkeypatch.setattr(sys, "argv", argv)
    assert await cli.main() == 8
    assert json.loads(capsys.readouterr().out)["exit_code"] == 8
    assert factory.create_calls == 0


@pytest.mark.parametrize("operation", ["messages", "embeddings"])
def test_acl_blocks_both_inference_writes_in_readonly(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    cfg = SimpleNamespace(global_readonly=False, global_metadata_only=False)
    resource = "anthropic_model" if operation == "messages" else "open_ai_model"
    with pytest.raises(AccessControlError):
        AccessControlGuard(cfg, "LANGUAGE_MODELS", str(cli._METADATA_ALLOWLIST_PATH)).check(resource, operation)


def test_acl_canonical_operation_override_permits_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    monkeypatch.setenv(
        "FOUNDRY_AGENTIC_CLI_LANGUAGE_MODELS_ANTHROPIC_MODEL_MESSAGES_READONLY", "false"
    )
    cfg = SimpleNamespace(global_readonly=False, global_metadata_only=False)
    assert AccessControlGuard(cfg, "LANGUAGE_MODELS", str(cli._METADATA_ALLOWLIST_PATH)).check("anthropic_model", "messages") is None


def test_packaged_tier_three_policy_permits_zero_of_two() -> None:
    cfg = SimpleNamespace(global_readonly=False, global_metadata_only=True)
    guard = AccessControlGuard(cfg, "LANGUAGE_MODELS", str(cli._METADATA_ALLOWLIST_PATH))
    blocked = 0
    for spec in cli.OP_SPECS:
        with pytest.raises(AccessControlError):
            guard.check(spec["resource"], spec["operation"])
        blocked += 1
    assert blocked == 2


def test_missing_policy_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    cfg = SimpleNamespace(global_readonly=False, global_metadata_only=False)
    guard = AccessControlGuard(cfg, "LANGUAGE_MODELS", str(tmp_path / "missing.md"))
    with pytest.raises(AccessControlError):
        guard.check("anthropic_model", "messages")


@pytest.mark.parametrize(
    ("exception", "exit_code"),
    [(BadRequestError({}), 1), (UnauthorizedError({}), 2), (ServiceUnavailable("x", "y"), 6)],
)
def test_actual_sdk_errors_use_safe_adr_envelopes(
    exception: Exception,
    exit_code: int,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert cli._serialize_error(exception) == exit_code
    output = capsys.readouterr().out
    assert json.loads(output)["exit_code"] == exit_code
    assert str(exception) not in caplog.text


def test_model_adapter_preserves_response_content() -> None:
    model = SimpleNamespace(to_dict=lambda: {"content": [{"type": "text", "text": "answer"}], "data": [[1.0]]})
    assert cli._model_to_dict(model)["data"] == [[1.0]]


@pytest.mark.asyncio
async def test_real_sdk_validation_error_does_not_expose_rejected_prompt_value(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "PROMPT_SYSTEM_TOOL_VECTOR_ATTRIBUTION_SECRET"
    client = AsyncFoundryClient(
        auth=UserTokenAuth("test-token"),
        hostname="https://example.invalid",
        preview=True,
    ).language_models.AnthropicModel
    args = argparse.Namespace(
        model_id="model",
        max_tokens=1,
        messages=[{"role": sentinel, "content": []}],
        output_config=None,
        stop_sequences=None,
        system=None,
        temperature=None,
        thinking=None,
        tool_choice=None,
        tools=None,
        top_k=None,
        top_p=None,
    )
    with pytest.raises(ValueError) as captured:
        await cli._invoke_sdk(cli._spec_for("anthropic_model", "messages"), client, args, 1)
    assert sentinel in str(captured.value)

    assert cli._serialize_error(captured.value) == 1
    streams = capsys.readouterr()
    envelope = json.loads(streams.out)
    assert envelope["exit_code"] == 1
    assert envelope["message"] == "Language Models operation failed"
    assert envelope["traceback"] == ""
    assert sentinel not in streams.out
    assert sentinel not in streams.err
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_attribution_scopes_are_isolated_and_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tracing:
        def __init__(self, config: Any) -> None:
            pass

        def scope(self, supplied: Any) -> _Scope:
            return _Scope()

    monkeypatch.setattr(factory_module, "TracingProvider", Tracing)
    outer_token = ATTRIBUTION_VAR.set(["outer"])
    ready = asyncio.Event()
    observed: dict[str, Any] = {}

    async def run(name: str, rid: str) -> None:
        cfg = SimpleNamespace(enable_attribution=True, attribution_rids=rid)
        with AsyncClientFactory().invocation_scope(cfg, include_attribution=True):
            observed[name] = ATTRIBUTION_VAR.get()
            ready.set()
            await ready.wait()
            assert ATTRIBUTION_VAR.get() == [rid]
        observed[f"{name}_restored"] = ATTRIBUTION_VAR.get()

    await asyncio.gather(run("first", "rid-1"), run("second", "rid-2"))
    assert observed["first"] == ["rid-1"]
    assert observed["second"] == ["rid-2"]
    assert observed["first_restored"] == ["outer"]
    assert observed["second_restored"] == ["outer"]
    ATTRIBUTION_VAR.reset(outer_token)
