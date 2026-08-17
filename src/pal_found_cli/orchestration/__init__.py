"""Foundry Orchestration command-line namespace."""

from pal_found_cli.orchestration.scripts.pal_found_orchestration_cli import (
    build_parser,
    console_main,
    main,
)

__all__ = ["build_parser", "console_main", "main"]
