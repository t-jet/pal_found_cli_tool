"""Approved TOON encoder and output selection contracts."""

import json
import sys
from pathlib import Path

import pytest
import toon_format

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pal_found_cli.common.output_formatter import ENV_DEFAULT_FORMAT, OutputFormatter


ROWS = [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Bob"}]
ROWS_TOON = "[2]{id,name}:\n  1,Ada\n  2,Bob"


@pytest.mark.parametrize("environment", ["json", "toon", "auto"])
@pytest.mark.parametrize("explicit", [None, "json", "toon", "auto"])
def test_explicit_selection_and_environment_default(environment, explicit, monkeypatch):
    monkeypatch.setenv(ENV_DEFAULT_FORMAT, environment)
    output = OutputFormatter(explicit).format(ROWS)
    effective = environment if explicit is None else explicit
    if effective == "json":
        assert json.loads(output) == ROWS
    else:
        assert output == ROWS_TOON


def test_unset_environment_uses_auto(monkeypatch):
    monkeypatch.delenv(ENV_DEFAULT_FORMAT, raising=False)
    assert OutputFormatter().format(ROWS) == ROWS_TOON


@pytest.mark.parametrize("setting", ["auto", "toon"])
@pytest.mark.parametrize(
    "data",
    [
        {"id": 1},
        [],
        [{"id": 1}, "scalar"],
        [{"id": 1}, {"name": "Ada"}],
        [[1], [2]],
        {"error": True, "message": "failed"},
        {"path": "file.bin", "size": 3, "checksum": "abc", "truncated": False},
    ],
)
def test_nonuniform_and_metadata_results_use_json(setting, data):
    assert json.loads(OutputFormatter(setting).format(data)) == data


@pytest.mark.parametrize("setting", ["auto", "json", "toon"])
def test_error_and_pagination_metadata_stay_json(setting, capsys):
    formatter = OutputFormatter(setting)
    error = {"error": True, "message": "failed"}
    assert json.loads(formatter.format_error(error)) == error
    metadata = {"nextPageToken": "next", "pages": 1}
    formatter.emit_to_stderr(metadata)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == metadata


@pytest.mark.parametrize(
    "data, expected",
    [
        ([{"name": "Ada", "active": True, "note": None}],
         "[1]{name,active,note}:\n  Ada,true,null"),
        ([{"text": 'a,b "quoted"\nnext', "city": "София"}],
         '[1]{text,city}:\n  "a,b \\"quoted\\"\\nnext",София'),
        ([{"id": 1, "meta": {"a": True}, "tags": ["x", "y"]}],
         "[1]:\n  - id: 1\n    meta:\n      a: true\n    tags[2]: x,y"),
        ([{"id": 1, "value": 1.5}, {"value": 2.5, "id": 2}],
         "[2]{id,value}:\n  1,1.5\n  2,2.5"),
    ],
)
def test_actual_encoder_matches_toon_fixtures(data, expected):
    output = OutputFormatter("toon").format(data)
    assert output == expected
    assert toon_format.loads(output) == data


def test_unexpected_encoder_failure_emits_json_without_exception_details(monkeypatch, capsys):
    def fail_encode(data):
        raise RuntimeError("sensitive-encoder-detail")

    monkeypatch.setattr(toon_format, "dumps", fail_encode)
    assert json.loads(OutputFormatter("toon").format(ROWS)) == ROWS
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "TOON encoding failed; emitting JSON.\n"
