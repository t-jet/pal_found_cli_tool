"""Public parser contracts across every console namespace."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import toon_format

from pal_found_cli.common.output_formatter import ENV_DEFAULT_FORMAT, OutputFormatter

NAMESPACES = (
    "admin", "aip_agents", "audit", "checkpoints", "connectivity", "data_health",
    "datasets", "filesystem", "functions", "language_models", "media_sets", "models",
    "ontologies", "orchestration", "sql_queries", "streams", "third_party_applications", "widgets",
)


def _module(namespace):
    return importlib.import_module(f"pal_found_cli.{namespace}.scripts.pal_found_{namespace}_cli")


def _first_operation(parser):
    path = []
    while True:
        sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None:
            return path, parser
        name, parser = next(iter(sub.choices.items()))
        path.append(name)


def _required_arguments(parser):
    values = []
    for action in parser._actions:
        if action.dest == "help" or (action.option_strings and not action.required):
            continue
        value = str(action.choices[0]) if action.choices else "synthetic"
        if action.type is int:
            value = "1"
        if action.option_strings:
            values.extend([action.option_strings[0], value])
        else:
            values.append(value)
    return values


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_format_default_defers_to_environment(namespace, monkeypatch):
    module = _module(namespace)
    path, leaf = _first_operation(module.build_parser())
    argv = path + _required_arguments(leaf)
    for environment in ("json", "toon", "auto"):
        monkeypatch.setenv(ENV_DEFAULT_FORMAT, environment)
        parsed = module.build_parser().parse_args(argv)
        assert parsed.format is None
        rows = [{"id": 1}, {"id": 2}]
        output = OutputFormatter(parsed.format).format(rows)
        assert (json.loads(output) if environment == "json" else toon_format.loads(output)) == rows
        for explicit in ("json", "toon", "auto"):
            selected = module.build_parser().parse_args([*argv, "--format", explicit]).format
            assert selected == explicit
            output = OutputFormatter(selected).format(rows)
            assert (json.loads(output) if explicit == "json" else toon_format.loads(output)) == rows


# Exercise the installed console functions in a child interpreter. Configuration,
# client construction, and network sockets are forbidden before parser rejection.
_RUNNER = r'''
import asyncio, contextlib, importlib, io, json, socket, sys
namespace, cases = json.loads(sys.argv[1])
module = importlib.import_module(f"pal_found_cli.{namespace}.scripts.pal_found_{namespace}_cli")
def forbidden(*args, **kwargs):
    raise AssertionError("Parsing reached configuration, SDK, or network")
module.ConfigLoader = forbidden
module.AsyncClientFactory = forbidden
socket.create_connection = forbidden
original_connect = socket.socket.connect
def connect(sock, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
        return original_connect(sock, address)
    return forbidden(sock, address)
socket.socket.connect = connect
entry = module.main if namespace == "datasets" else module.console_main
results = []
for argv in cases:
    sys.argv = ["pal-found-" + namespace.replace("_", "-"), *argv]
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = entry()
        except SystemExit as exc:
            code = exc.code
    results.append({"code": code, "out": out.getvalue(), "err": err.getvalue()})
print(json.dumps(results))
'''


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_console_parse_errors_are_sanitized_json_before_sdk(namespace):
    path, leaf = _first_operation(_module(namespace).build_parser())
    required = _required_arguments(leaf)
    base = path + required
    secret = "secret-marker-do-not-echo"
    errors = [[], path[:1], [secret], [*base, "--" + secret],
              [*base, "--format", secret], [*base, "--timeout", secret]]
    if required:
        errors.append(path)
    cases = errors + [["--help"], [*path, "--help"]]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(Path(__file__).resolve().parents[1] / "src"), env.get("PYTHONPATH"),
    ]))
    result = subprocess.run([sys.executable, "-c", _RUNNER, json.dumps([namespace, cases])],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    for record in records[:len(errors)]:
        assert record["code"] == 1, record
        envelope = json.loads(record["out"])
        assert envelope["exit_code"] == 1
        assert secret not in record["out"] + record["err"]
        assert "usage:" not in record["err"].lower()
    for record in records[len(errors):]:
        assert record["code"] == 0
        assert "usage:" in record["out"].lower()
        assert record["err"] == ""


def test_build_search_requires_filter_before_configuration(monkeypatch, capsys):
    module = _module("orchestration")
    monkeypatch.setattr(sys, "argv", ["cmd", "build", "search"])
    monkeypatch.setattr(module, "ConfigLoader", lambda: pytest.fail("Configuration reached"))
    assert module.console_main() == 1
    assert json.loads(capsys.readouterr().out)["exit_code"] == 1
