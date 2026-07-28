#!/usr/bin/env python3
"""Unit tests for Foundry Ontologies CLI."""

import argparse
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_SKILL_DIR = Path(__file__).parent.parent / ".claude" / "skills" / "foundry-ontologies" / "scripts"
sys.path.insert(0, str(_SKILL_DIR))

import importlib

foundry_ontologies_cli = importlib.import_module("foundry_ontologies_cli")


def _ns(**kwargs):
    base = {
        "timeout": None,
        "format": "auto",
        "pretty": False,
        "page_size": None,
        "page_token": None,
        "batch_pages": None,
        "output_filename": None,
        "content_length": None,
        "content_type": None,
        "body_file": None,
    }
    for spec in foundry_ontologies_cli.OP_SPECS:
        for name in spec["positional"] + spec["optional"]:
            base.setdefault(name, None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _value_for(name):
    if name == "content_length":
        return 123
    if name in {"requests", "parameters", "options", "overrides", "request", "object_set", "links"}:
        return '{"x": 1}'
    if name in {
        "select",
        "select_v2",
        "order_by",
        "where",
        "aggregation",
        "group_by",
        "range",
        "filters",
        "edits",
        "action_types",
        "interface_types",
        "link_types",
        "object_types",
        "query_types",
        "object_type_api_names",
        "attribution",
        "augmented_interface_property_types",
        "augmented_properties",
        "augmented_shared_property_types",
        "other_interface_types",
        "selected_interface_property_types",
        "selected_object_types",
        "selected_shared_property_types",
    }:
        return '["x"]'
    if name == "aggregate":
        return '{"type": "exact"}'
    if name in {
        "preview",
        "snapshot",
        "exclude_rid",
        "include_compute_usage",
        "load_property_securities",
        "include_all_previous_properties",
    }:
        return True
    if name == "page_size":
        return 25
    if name == "page_token":
        return "tok"
    return f"{name}-value"


def _args_for(spec, **overrides):
    values = {name: _value_for(name) for name in spec["positional"] + spec["optional"]}
    values.update(overrides)
    return _ns(**values)


def _mock_root():
    root = MagicMock()
    for spec in foundry_ontologies_cli.OP_SPECS:
        client = root
        for attr in spec["client_path"].split("."):
            client = getattr(client, attr)
        setattr(client, spec["method"], AsyncMock(return_value={"ok": True}))
    return root


def test_operation_catalog_has_67_unique_operations():
    paths = {(spec["resource"], spec["operation"]) for spec in foundry_ontologies_cli.OP_SPECS}
    assert len(foundry_ontologies_cli.OP_SPECS) == 67
    assert len(paths) == 67


@pytest.mark.parametrize("spec", foundry_ontologies_cli.OP_SPECS)
def test_parser_accepts_every_canonical_operation(spec):
    parser = foundry_ontologies_cli.build_parser()
    argv = [spec["resource"].replace("_", "-"), spec["operation"].replace("_", "-")]
    for arg in spec["positional"]:
        argv.append(_value_for(arg))
    argv.extend(["--timeout", "9", "--format", "json"])
    args = parser.parse_args(argv)
    assert args.resource == spec["resource"].replace("_", "-")
    assert args.operation == spec["operation"].replace("_", "-")
    assert args.timeout == 9
    assert args.format == "json"


@pytest.mark.parametrize("spec", foundry_ontologies_cli.OP_SPECS)
@pytest.mark.asyncio
async def test_invoke_dispatches_every_canonical_operation(spec, tmp_path):
    client = MagicMock()
    method = AsyncMock(return_value={"ok": True})
    setattr(client, spec["method"], method)
    args = _args_for(spec)
    if spec["binary_upload"]:
        body_file = tmp_path / "body.bin"
        body_file.write_bytes(b"payload")
        args.body_file = str(body_file)
    if spec["binary_download"]:
        method.return_value = b"payload"
        handler = MagicMock()
        handler.save = AsyncMock(return_value=MagicMock(to_dict=lambda: {"file_path": "x", "file_size": 7}))
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(foundry_ontologies_cli, "BinaryDownloadHandler", lambda config=None: handler)
        try:
            result = await foundry_ontologies_cli._invoke(
                spec["resource"], spec["operation"], client, args, timeout=5, cfg=MagicMock()
            )
        finally:
            monkeypatch.undo()
        assert result["file_size"] == 7
    else:
        await foundry_ontologies_cli._invoke(
            spec["resource"], spec["operation"], client, args, timeout=5, cfg=MagicMock()
        )
    assert method.await_count == 1
    assert method.call_args.kwargs["request_timeout"] == 5


def test_get_client_routes_nested_and_root_clients(monkeypatch):
    sdk = MagicMock()
    sdk.ontologies = _mock_root()
    factory = MagicMock()
    factory.create.return_value = sdk
    cfg = MagicMock()

    assert foundry_ontologies_cli._get_client(cfg, "action", factory) == sdk.ontologies.Action
    assert foundry_ontologies_cli._get_client(cfg, "action_type", factory) == sdk.ontologies.Ontology.ActionType
    assert foundry_ontologies_cli._get_client(cfg, "query_type", factory) == sdk.ontologies.Ontology.QueryType


def test_paginated_catalog_uses_page_size_and_token_specs():
    expected = {
        (spec["resource"], spec["operation"])
        for spec in foundry_ontologies_cli.OP_SPECS
        if "page_size" in spec["optional"] and "page_token" in spec["optional"]
    }
    assert foundry_ontologies_cli.PAGINATED_OPS == expected
    assert ("ontology_object", "list") in expected
    assert ("query_type", "list") in expected


@pytest.mark.asyncio
async def test_invoke_paginated_batches_pages():
    client = MagicMock()
    client.list = AsyncMock(side_effect=[
        {"items": [{"id": 1}], "next_page_token": "next"},
        {"items": [{"id": 2}]},
    ])
    args = _ns(ontology="ont", object_type="obj", page_size=1, page_token=None)
    helper = foundry_ontologies_cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await foundry_ontologies_cli._invoke_paginated(
        "ontology_object", "list", client, args, 3, helper, MagicMock()
    )

    assert result == [{"id": 1}, {"id": 2}]
    assert client.list.await_count == 2
    assert helper.pages_fetched == 2


@pytest.mark.asyncio
async def test_invoke_accepts_nonawaitable_sdk_iterator_result():
    client = MagicMock()
    response = {"items": [{"id": 1}], "next_page_token": None}
    client.list = MagicMock(return_value=response)
    args = _ns(ontology="ont", object_type="obj", page_size=1, page_token=None)

    result = await foundry_ontologies_cli._invoke(
        "ontology_object",
        "list",
        client,
        args,
        timeout=3,
        cfg=MagicMock(),
    )

    assert result == response
    client.list.assert_called_once()


@pytest.mark.asyncio
async def test_binary_upload_reads_body_file_and_sets_attachment_headers(tmp_path):
    client = MagicMock()
    client.upload = AsyncMock(return_value={"rid": "a"})
    body_file = tmp_path / "a.bin"
    body_file.write_bytes(b"abc")
    args = _args_for(
        foundry_ontologies_cli.OPERATION_BY_RESOURCE["attachment"]["upload"],
        body_file=str(body_file),
        content_length=None,
        content_type=None,
    )

    await foundry_ontologies_cli._invoke("attachment", "upload", client, args, 8, MagicMock())

    assert client.upload.call_args.args == (b"abc",)
    assert client.upload.call_args.kwargs["content_length"] == 3
    assert client.upload.call_args.kwargs["content_type"] == "application/octet-stream"


@pytest.mark.asyncio
async def test_attachment_upload_requires_filename(tmp_path):
    client = MagicMock()
    client.upload = AsyncMock(return_value={"rid": "a"})
    body_file = tmp_path / "a.bin"
    body_file.write_bytes(b"abc")
    args = _args_for(
        foundry_ontologies_cli.OPERATION_BY_RESOURCE["attachment"]["upload"],
        body_file=str(body_file),
        filename=None,
    )

    with pytest.raises(ValueError, match="filename is required"):
        await foundry_ontologies_cli._invoke("attachment", "upload", client, args, 8, MagicMock())


@pytest.mark.asyncio
async def test_media_upload_does_not_send_attachment_headers(tmp_path):
    client = MagicMock()
    client.upload = AsyncMock(return_value={"rid": "m"})
    body_file = tmp_path / "m.bin"
    body_file.write_bytes(b"abc")
    spec = foundry_ontologies_cli.OPERATION_BY_RESOURCE["media_reference_property"]["upload"]
    args = _args_for(spec, body_file=str(body_file))

    await foundry_ontologies_cli._invoke("media_reference_property", "upload", client, args, 8, MagicMock())

    assert "content_length" not in client.upload.call_args.kwargs
    assert "content_type" not in client.upload.call_args.kwargs


@pytest.mark.asyncio
async def test_binary_download_uses_handler(monkeypatch):
    client = MagicMock()
    client.read = AsyncMock(return_value=b"abc")
    handler = MagicMock()
    handler.save = AsyncMock(return_value=MagicMock(to_dict=lambda: {"file_path": "x", "file_size": 3}))
    monkeypatch.setattr(foundry_ontologies_cli, "BinaryDownloadHandler", lambda config=None: handler)

    result = await foundry_ontologies_cli._invoke(
        "attachment", "read", client, _ns(attachment_rid="rid"), 4, MagicMock()
    )

    assert result == {"file_path": "x", "file_size": 3}
    handler.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_binary_download_accepts_sync_chunk_iterator(monkeypatch):
    client = MagicMock()
    client.read = AsyncMock(return_value=iter([b"a", b"b"]))
    saved = {}

    class Handler:
        async def save(self, stream, **kwargs):
            saved["chunks"] = [chunk async for chunk in stream]
            return MagicMock(to_dict=lambda: {"file_size": 2})

    monkeypatch.setattr(foundry_ontologies_cli, "BinaryDownloadHandler", lambda config=None: Handler())

    result = await foundry_ontologies_cli._invoke(
        "attachment", "read", client, _ns(attachment_rid="rid"), 4, MagicMock()
    )

    assert result == {"file_size": 2}
    assert saved["chunks"] == [b"a", b"b"]


@pytest.mark.asyncio
async def test_main_success_uses_acl_retry_output_and_b3_scope(monkeypatch, capsys):
    client = MagicMock()
    client.get = AsyncMock(return_value={"rid": "x"})
    entered = {"scope": False}

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    entered["scope"] = True
                def __exit__(self, exc_type, exc, tb):
                    return False
            return Scope()
        def create(self, cfg):
            sdk = MagicMock()
            sdk.ontologies.Ontology = client
            return sdk

    class Cfg:
        timeout_s = 30
        log_level = "INFO"
        def load(self):
            return None

    guard = MagicMock()
    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_ontologies_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_ontologies_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AccessControlGuard", lambda cfg, ns: guard)
    monkeypatch.setattr(foundry_ontologies_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_ontologies_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "ontology", "get", "ontology-rid", "--format", "json"])

    rc = await foundry_ontologies_cli.main()

    assert rc == foundry_ontologies_cli.EXIT_SUCCESS
    guard.check.assert_called_once_with("ontology", "get")
    assert retry.execute.await_count == 1
    assert entered["scope"] is True
    assert "rid" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_acl_denied_returns_exit_8(monkeypatch):
    class Cfg:
        timeout_s = 30
        log_level = "INFO"
        def load(self):
            return None

    monkeypatch.setattr(foundry_ontologies_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_ontologies_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_ontologies_cli,
        "AccessControlGuard",
        lambda cfg, ns: MagicMock(check=MagicMock(side_effect=foundry_ontologies_cli.AccessControlError("blocked"))),
    )
    monkeypatch.setattr(sys, "argv", ["prog", "ontology", "get", "ontology-rid"])

    rc = await foundry_ontologies_cli.main()

    assert rc == foundry_ontologies_cli.EXIT_ACCESS_CONTROL


@pytest.mark.asyncio
async def test_main_user_input_error_returns_exit_1(monkeypatch):
    class Cfg:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            return None

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return Scope()

        def create(self, cfg):
            sdk = MagicMock()
            sdk.ontologies.Attachment = MagicMock()
            return sdk

    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=ValueError("filename is required"))
    monkeypatch.setattr(foundry_ontologies_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_ontologies_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AccessControlGuard", lambda cfg, ns: MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_ontologies_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "attachment", "upload"])

    rc = await foundry_ontologies_cli.main()

    assert rc == foundry_ontologies_cli.EXIT_USER_INPUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,exit_code",
    [
        (PermissionError("403"), foundry_ontologies_cli.EXIT_PERMISSION_DENIED),
        (FileNotFoundError("missing"), foundry_ontologies_cli.EXIT_NOT_FOUND),
        (TimeoutError("slow"), foundry_ontologies_cli.EXIT_TIMEOUT),
        (RuntimeError("boom"), foundry_ontologies_cli.EXIT_SERVER_ERROR),
    ],
)
async def test_main_error_serialization_paths(monkeypatch, exc, exit_code):
    class Cfg:
        timeout_s = 30
        log_level = "INFO"
        def load(self):
            return None

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    return None
                def __exit__(self, exc_type, exc, tb):
                    return False
            return Scope()
        def create(self, cfg):
            sdk = MagicMock()
            sdk.ontologies.Ontology = MagicMock()
            return sdk

    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=exc)
    monkeypatch.setattr(foundry_ontologies_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_ontologies_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AccessControlGuard", lambda cfg, ns: MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_ontologies_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "ontology", "get", "ontology-rid"])

    rc = await foundry_ontologies_cli.main()

    assert rc == exit_code


def test_skill_text_is_b3_only():
    text = (Path(__file__).parent.parent / ".claude" / "skills" / "foundry-ontologies" / "SKILL.md").read_text()
    assert "B3" in text
    assert "W3C" not in text


def test_output_formatter_supports_json_and_toon():
    json_out = foundry_ontologies_cli.OutputFormatter(format_setting="json").format({"key": "value"})
    toon_out = foundry_ontologies_cli.OutputFormatter(format_setting="auto").format([
        {"a": 1, "b": 2},
        {"a": 3, "b": 4},
    ])
    assert '"key": "value"' in json_out
    assert "a" in toon_out and "b" in toon_out


def test_resolve_converts_kebab_to_snake():
    assert foundry_ontologies_cli._resolve("object-type", "get-full-metadata") == "get_full_metadata"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
