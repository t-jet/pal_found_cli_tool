"""Argument parsing errors handled by the CLI JSON error boundary."""

import argparse
from typing import NoReturn


class CLIInputError(ValueError):
    """Invalid command input with a message that omits argument values."""

    exit_code = 1


class ArgumentParser(argparse.ArgumentParser):
    """Raise input errors while preserving argparse help and version exits."""

    def error(self, message: str) -> NoReturn:
        raise CLIInputError("Invalid command input") from None
