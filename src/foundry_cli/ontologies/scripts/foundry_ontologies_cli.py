"""Console entry point wrapper for the Foundry Ontologies CLI."""

from __future__ import annotations

import asyncio
import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import cast


_LEGACY_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / ".claude"
    / "skills"
    / "foundry-ontologies"
    / "scripts"
    / "foundry_ontologies_cli.py"
)


@lru_cache(maxsize=1)
def _load_legacy_cli() -> ModuleType:
    """Load the maintained Foundry Ontologies CLI implementation."""
    if not _LEGACY_SCRIPT.exists():
        raise ImportError(f"Foundry Ontologies CLI implementation not found: {_LEGACY_SCRIPT}")

    spec = importlib.util.spec_from_file_location(
        "_foundry_ontologies_cli_impl",
        _LEGACY_SCRIPT,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load Foundry Ontologies CLI implementation: {_LEGACY_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def async_main() -> int:
    """Run the async Foundry Ontologies CLI implementation."""
    legacy_cli = _load_legacy_cli()
    return cast(int, await legacy_cli.main())


def main() -> int:
    """Run the Foundry Ontologies CLI from the console script entry point."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
