"""Tests for the 20-operation Foundry Orchestration CLI.

Covers the exact catalog (no ScheduleRun entries), parser surface, nested SDK
routing and dispatch, JSON validation, the three cursor-paged commands,
single-call batch operations, access control write classification and
read-only mode, the packaged 12/8 metadata-only policy, retry and error
taxonomy, output formats, tracing, and the console entry point.

All SDK transport is mocked: no live Foundry connection is ever made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundry_cli.orchestration.scripts import foundry_orchestration_cli as cli


class _Scope:
    """No-op async invocation scope double."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> bool:
        return False


class _Factory:
    """Recording factory double that exposes invocation_scope and create."""

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
    """Retry double that executes the wrapped callable exactly once."""

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


def _root() -> tuple[Any, dict[str, AsyncMock]]:
    """Build a nested Orchestration SDK fake rooted at client.orchestration."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    build = SimpleNamespace(
        cancel=tracked("build_cancel"),
        create=tracked("build_create"),
        get=tracked("build_get"),
        get_batch=tracked("build_get_batch"),
        jobs=tracked("build_jobs"),
        search=tracked("build_search"),
    )
    job = SimpleNamespace(
        get=tracked("job_get"),
        get_batch=tracked("job_get_batch"),
    )
    schedule = SimpleNamespace(
        create=tracked("schedule_create"),
        delete=tracked("schedule_delete"),
        get=tracked("schedule_get"),
        get_affected_resources=tracked("schedule_get_affected_resources"),
        get_batch=tracked("schedule_get_batch"),
        pause=tracked("schedule_pause"),
        replace=tracked("schedule_replace"),
        run=tracked("schedule_run"),
        runs=tracked("schedule_runs"),
        unpause=tracked("schedule_unpause"),
    )
    schedule_version = SimpleNamespace(
        get=tracked("schedule_version_get"),
        schedule=tracked("schedule_version_schedule"),
    )
    root = SimpleNamespace(
        orchestration=SimpleNamespace(
            Build=build,
            Job=job,
            Schedule=schedule,
            ScheduleVersion=schedule_version,
            # ScheduleRun intentionally absent: no public SDK methods.
        )
    )
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_20_operations_no_schedule_run() -> None:
    assert len(cli.OP_SPECS) == 20
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 20
    resources = {spec["resource"] for spec in cli.OP_SPECS}
    assert resources == {"build", "job", "schedule", "schedule_version"}
    assert "schedule_run" not in resources
    assert "schedule_run" not in cli.OPERATION_BY_RESOURCE
    counts: dict[str, int] = {}
    for spec in cli.OP_SPECS:
        counts[spec["resource"]] = counts.get(spec["resource"], 0) + 1
    assert counts == {"build": 6, "job": 2, "schedule": 10, "schedule_version": 2}


def test_catalog_dispatch_paths_exist() -> None:
    for spec in cli.OP_SPECS:
        assert spec["client_path"] in {
            ("Build",),
            ("Job",),
            ("Schedule",),
            ("ScheduleVersion",),
        }


def test_catalog_marks_exactly_three_paged_operations() -> None:
    assert cli.PAGINATED_OPS == frozenset(
        {
            ("build", "jobs"),
            ("build", "search"),
            ("schedule", "runs"),
        }
    )


def test_catalog_marks_exactly_eight_write_operations() -> None:
    assert cli.WRITE_OPS == frozenset(
        {
            ("build", "cancel"),
            ("build", "create"),
            ("schedule", "create"),
            ("schedule", "delete"),
            ("schedule", "pause"),
            ("schedule", "replace"),
            ("schedule", "run"),
            ("schedule", "unpause"),
        }
    )


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["build", "cancel", "ri.b"])
    _parse(
        [
            "build",
            "create",
            "--target-json",
            "{}",
            "--fallback-branches-json",
            "[]",
            "--force-build",
            "--retry-count",
            "2",
            "--branch-name",
            "main",
        ]
    )
    _parse(["build", "get", "ri.b"])
    _parse(["build", "get-batch", "--build-rids-json", '["ri.b"]'])
    _parse(["build", "jobs", "ri.b", "--page-size", "5", "--page-token", "t", "--all"])
    _parse(["build", "search", "--where-json", "{}", "--max-pages", "3"])
    _parse(["job", "get", "ri.j"])
    _parse(["job", "get-batch", "--job-rids-json", '["ri.j"]'])
    _parse(
        [
            "schedule",
            "create",
            "--action-json",
            "{}",
            "--trigger-json",
            "{}",
            "--scope-mode-json",
            "{}",
            "--display-name",
            "nightly",
            "--description",
            "desc",
        ]
    )
    _parse(["schedule", "delete", "ri.s"])
    _parse(["schedule", "get", "ri.s"])
    _parse(["schedule", "get-affected-resources", "ri.s"])
    _parse(["schedule", "get-batch", "--schedule-rids-json", '["ri.s"]'])
    _parse(["schedule", "pause", "ri.s"])
    _parse(
        [
            "schedule",
            "replace",
            "ri.s",
            "--action-json",
            "{}",
            "--trigger-json",
            "{}",
            "--scope-mode-json",
            "{}",
        ]
    )
    _parse(["schedule", "run", "ri.s"])
    _parse(["schedule", "runs", "ri.s", "--max-pages", "2"])
    _parse(["schedule", "unpause", "ri.s"])
    _parse(["schedule-version", "get", "ri.sv"])
    _parse(["schedule-version", "schedule", "ri.sv"])


def test_unknown_client_or_operation_rejected() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["schedule-run", "list"])
    with pytest.raises(cli.CLIInputError):
        _parse(["build", "bogus"])
    with pytest.raises(cli.CLIInputError):
        _parse(["nope", "get"])


def test_non_paged_commands_reject_pagination_flags() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["build", "get", "ri.b", "--page-size", "5"])
    with pytest.raises(cli.CLIInputError):
        _parse(["job", "get", "ri.j", "--all"])
    with pytest.raises(cli.CLIInputError):
        _parse(["schedule", "get", "ri.s", "--max-pages", "2"])


def test_help_exits_zero_and_names_clients(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for client in ("build", "job", "schedule", "schedule-version"):
        assert client in out


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


def test_json_arguments_decode_before_dispatch() -> None:
    args = _parse(
        [
            "schedule",
            "create",
            "--action-json",
            '{"type": "build"}',
            "--trigger-json",
            '{"type": "manual"}',
            "--scope-mode-json",
            '{"scope": "project"}',
        ]
    )
    spec = cli._spec_for("schedule", "create")
    cli._validate_inputs(spec, args)
    assert args.action == {"type": "build"}
    assert args.trigger == {"type": "manual"}
    assert args.scope_mode == {"scope": "project"}


def test_invalid_json_rejected_before_client() -> None:
    args = _parse(
        [
            "schedule",
            "create",
            "--action-json",
            "{",
            "--trigger-json",
            "{}",
            "--scope-mode-json",
            "{}",
        ]
    )
    spec = cli._spec_for("schedule", "create")
    with pytest.raises(cli.CLIInputError, match="valid JSON"):
        cli._validate_inputs(spec, args)


def test_wrong_json_shape_rejected_without_echo() -> None:
    sentinel = "secret-payload"
    args = _parse(
        [
            "build",
            "create",
            "--target-json",
            json.dumps([sentinel]),
            "--fallback-branches-json",
            "[]",
        ]
    )
    spec = cli._spec_for("build", "create")
    with pytest.raises(cli.CLIInputError) as captured:
        cli._validate_inputs(spec, args)
    assert sentinel not in str(captured.value)
    with pytest.raises(cli.CLIInputError, match="JSON object"):
        cli._validate_inputs(spec, args)


@pytest.mark.asyncio
async def test_invalid_json_returns_exit_one_without_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "schedule",
            "create",
            "--action-json",
            "{",
            "--trigger-json",
            "{}",
            "--scope-mode-json",
            "{}",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Nested routing and dispatch
# --------------------------------------------------------------------------


def test_get_client_uses_exact_nested_routes() -> None:
    root, _ = _root()
    specs = [
        (("Build",), root.orchestration.Build),
        (("Job",), root.orchestration.Job),
        (("Schedule",), root.orchestration.Schedule),
        (("ScheduleVersion",), root.orchestration.ScheduleVersion),
    ]
    for client_path, expected in specs:
        assert cli._get_client(root, client_path) is expected


@pytest.mark.asyncio
async def test_build_get_dispatch_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["build_get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.b"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "build", "get", "ri.b", "--format", "json"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.b"}
    calls["build_get"].assert_awaited_once_with("ri.b", request_timeout=30)
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


@pytest.mark.asyncio
async def test_schedule_run_dispatch_omits_absent_optionals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["schedule_run"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.r"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "schedule", "run", "ri.s", "--format", "json"])
    assert await cli.main() == 0
    calls["schedule_run"].assert_awaited_once_with("ri.s", request_timeout=30)


@pytest.mark.asyncio
async def test_schedule_create_forwards_parsed_json_and_optionals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["schedule_create"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.s"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "schedule",
            "create",
            "--action-json",
            '{"type": "build"}',
            "--trigger-json",
            '{"type": "manual"}',
            "--scope-mode-json",
            '{"scope": "project"}',
            "--display-name",
            "nightly",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["schedule_create"].assert_awaited_once_with(
        action={"type": "build"},
        trigger={"type": "manual"},
        scope_mode={"scope": "project"},
        display_name="nightly",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_build_create_optional_flags_omitted_when_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["build_create"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.b"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "build",
            "create",
            "--target-json",
            '{"targets": []}',
            "--fallback-branches-json",
            "[]",
            "--force-build",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["build_create"].assert_awaited_once_with(
        target={"targets": []},
        fallback_branches=[],
        force_build=True,
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_schedule_version_schedule_optional_result_null(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["schedule_version_schedule"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "schedule-version", "schedule", "ri.sv", "--format", "json"]
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) is None
    calls["schedule_version_schedule"].assert_awaited_once_with(
        "ri.sv", request_timeout=30
    )


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def _page_response(items: list[Any], next_token: str | None) -> Any:
    return SimpleNamespace(
        decode=lambda: SimpleNamespace(data=items, next_page_token=next_token)
    )


class _RawList:
    """Cursor-driven raw response double for a paged operation."""

    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        token = kwargs.get("page_token")
        if token is None:
            items, nxt = self.pages[0]
        else:
            for index, (_, nxt) in enumerate(self.pages):
                if nxt == token:
                    items, nxt = self.pages[index + 1]
                    break
            else:
                items, nxt = [], None
        return _page_response(items, nxt)


class _RawBuild:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.jobs = _RawList(pages)
        self.search = _RawList(pages)


@pytest.mark.asyncio
async def test_build_jobs_paginates_via_raw_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _RawBuild([(["j1", "j2"], "tok1"), (["j3"], None)])
    build = SimpleNamespace(with_raw_response=raw)
    root = SimpleNamespace(orchestration=SimpleNamespace(Build=build))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "build", "jobs", "ri.b", "--max-pages", "5", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == ["j1", "j2", "j3"]
    assert len(raw.jobs.calls) == 2
    assert raw.jobs.calls[1]["page_token"] == "tok1"


@pytest.mark.asyncio
async def test_build_search_defaults_single_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _RawBuild([([{"rid": "ri.b"}], None)])
    build = SimpleNamespace(with_raw_response=raw)
    root = SimpleNamespace(orchestration=SimpleNamespace(Build=build))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "build", "search", "--where-json", "{}", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [{"rid": "ri.b"}]
    assert len(raw.search.calls) == 1


@pytest.mark.asyncio
async def test_get_batch_is_single_call_no_paging(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["build_get_batch"].return_value = SimpleNamespace(
        to_dict=lambda: {"items": [{"rid": "ri.b"}]}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "build", "get-batch", "--build-rids-json", '["ri.b"]', "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"items": [{"rid": "ri.b"}]}
    calls["build_get_batch"].assert_awaited_once_with(
        body=[{"build_rid": "ri.b"}], request_timeout=30
    )


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_eight_mutating_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["build", "cancel", "ri.b"],
        ["build", "create", "--target-json", "{}", "--fallback-branches-json", "[]"],
        ["schedule", "create", "--action-json", "{}", "--trigger-json", "{}", "--scope-mode-json", "{}"],
        ["schedule", "delete", "ri.s"],
        ["schedule", "pause", "ri.s"],
        ["schedule", "replace", "ri.s", "--action-json", "{}", "--trigger-json", "{}", "--scope-mode-json", "{}"],
        ["schedule", "run", "ri.s"],
        ["schedule", "unpause", "ri.s"],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_semantic_reads_permitted_under_readonly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")

    raw = _RawBuild([([{"rid": "ri.b"}], None)])
    build = SimpleNamespace(with_raw_response=raw)
    schedule = SimpleNamespace(
        get_affected_resources=AsyncMock(
            return_value=SimpleNamespace(to_dict=lambda: {"resources": []})
        )
    )
    root = SimpleNamespace(
        orchestration=SimpleNamespace(Build=build, Schedule=schedule)
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "build", "search", "--where-json", "{}", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [{"rid": "ri.b"}]

    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "schedule", "get-affected-resources", "ri.s", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"resources": []}


@pytest.mark.asyncio
async def test_acl_denial_reports_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    monkeypatch.setattr(sys, "argv", ["cmd", "schedule", "run", "ri.s"])
    assert await cli.main() == 8
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 8
    assert "metadata-only mode active" in envelope["message"]


def test_metadata_only_permits_exactly_12_blocks_8() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("build", "get"),
        ("build", "get_batch"),
        ("build", "jobs"),
        ("build", "search"),
        ("job", "get"),
        ("job", "get_batch"),
        ("schedule", "get"),
        ("schedule", "get_affected_resources"),
        ("schedule", "get_batch"),
        ("schedule", "runs"),
        ("schedule_version", "get"),
        ("schedule_version", "schedule"),
    }
    allowed_path = Path(cli._METADATA_ALLOWLIST_PATH)
    assert allowed_path.exists()
    text = allowed_path.read_text(encoding="utf-8")
    parsed: set[tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if "PERMITTED" not in stripped:
            continue
        path = stripped.split("|")[1].strip().strip("`")
        resource, operation = path.split(".")[1:]
        parsed.add((resource, operation))
    assert parsed == permitted
    blocked = catalog - permitted
    assert blocked == {
        ("build", "cancel"),
        ("build", "create"),
        ("schedule", "create"),
        ("schedule", "delete"),
        ("schedule", "pause"),
        ("schedule", "replace"),
        ("schedule", "run"),
        ("schedule", "unpause"),
    }


# --------------------------------------------------------------------------
# Timeouts, errors, output, console
# --------------------------------------------------------------------------


def test_timeout_accepts_adr_002_bounds() -> None:
    assert cli._validate_timeout(1) == 1
    assert cli._validate_timeout(3600) == 3600
    for invalid in (0, 3601, -1, True):
        with pytest.raises(cli.CLIInputError):
            cli._validate_timeout(invalid)


@pytest.mark.asyncio
async def test_invalid_timeout_stops_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "build", "get", "ri.b", "--timeout", "0"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_error_maps_to_expected_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry_sdk._errors import (
        BadRequestError,
        NotFoundError,
        PermissionDeniedError,
        ServiceUnavailable,
        UnauthorizedError,
    )

    cases = [
        (BadRequestError({"errorName": "BadRequest", "errorInstanceId": "x"}), 1),
        (UnauthorizedError({"errorName": "Unauthorized", "errorInstanceId": "x"}), 2),
        (PermissionDeniedError({"errorName": "PermissionDenied", "errorInstanceId": "x"}), 3),
        (NotFoundError({"errorName": "NotFound", "errorInstanceId": "x"}), 4),
        (TimeoutError("slow"), 5),
        (ServiceUnavailable("503", "THROTTLED"), 6),
    ]
    for error, expected_code in cases:
        root, calls = _root()
        calls["build_get"].side_effect = error
        factory = _Factory(root)
        _patch_main(monkeypatch, factory)
        monkeypatch.setattr(sys, "argv", ["cmd", "build", "get", "ri.b"])
        assert await cli.main() == expected_code
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == expected_code


@pytest.mark.asyncio
async def test_no_secrets_in_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "sentinel-schedule-secret"
    root, calls = _root()
    calls["build_get"].side_effect = RuntimeError(sentinel)
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "build", "get", "ri.b"])
    assert await cli.main() == 6
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


@pytest.mark.asyncio
async def test_output_format_and_attribution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["build_get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.b"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "build", "get", "ri.b", "--format", "json", "--pretty"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.b"}
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


def test_console_main_uses_one_asyncio_run_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run(coro: Any) -> int:
        calls.append("run")
        return 7

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    monkeypatch.setattr(cli, "main", lambda: 7)
    assert cli.console_main() == 7
    assert calls == ["run"]


def test_claude_launcher_is_thin_and_reexports_packaged_interfaces() -> None:
    import importlib

    launcher = importlib.import_module(
        "foundry_cli.orchestration.scripts.foundry_orchestration_cli"
    )
    assert launcher is cli
