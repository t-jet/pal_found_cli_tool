"""Public parser failures stay inside the structured error boundary."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pal_found_cli.common.cli_parser import ArgumentParser, CLIInputError
from pal_found_cli.common.error_serializer import ErrorSerializer


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="contract-cli")
    parser.add_argument("--version", action="version", version="contract 1")
    resources = parser.add_subparsers(dest="resource", required=True)
    resource = resources.add_parser("record")
    operations = resource.add_subparsers(dest="operation", required=True)
    operation = operations.add_parser("get")
    operation.add_argument("rid")
    operation.add_argument("--limit", type=int)
    operation.add_argument("--format", choices=("auto", "json", "toon"))
    return parser


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["record"],
        ["record", "get"],
        ["unknown-secret-marker"],
        ["record", "get", "rid", "--unknown-secret-marker"],
        ["record", "get", "rid", "--limit", "secret-marker"],
        ["record", "get", "rid", "--format", "secret-marker"],
    ],
)
@pytest.mark.parametrize("include_traceback", ["false", "true"])
def test_nested_parser_errors_are_sanitized_json(arguments, include_traceback, capsys, monkeypatch):
    monkeypatch.setenv("FOUNDRY_INCLUDE_TRACEBACK", include_traceback)
    with pytest.raises(CLIInputError, match="^Invalid command input$") as caught:
        _parser().parse_args(arguments)
    assert caught.value.exit_code == 1
    assert capsys.readouterr() == ("", "")
    assert ErrorSerializer().serialize(caught.value) == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope
    assert "secret-marker" not in captured.out
    assert "usage:" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("arguments", [["--help"], ["record", "--help"], ["record", "get", "--help"]])
def test_help_preserves_successful_argparse_exit(arguments, capsys):
    with pytest.raises(SystemExit) as caught:
        _parser().parse_args(arguments)
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert captured.err == ""


def test_version_preserves_successful_argparse_exit(capsys):
    with pytest.raises(SystemExit) as caught:
        _parser().parse_args(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr() == ("contract 1\n", "")
