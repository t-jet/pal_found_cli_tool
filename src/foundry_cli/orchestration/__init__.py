"""Foundry Orchestration command-line namespace."""

from foundry_cli.orchestration.scripts.foundry_orchestration_cli import (
    build_parser,
    console_main,
    main,
)

__all__ = ["build_parser", "console_main", "main"]
