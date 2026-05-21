#!/usr/bin/env python3
"""Structured NDJSON logging setup for Foundry CLI (ADR-005).

Configures Python `logging` with a custom JSON formatter directed to stderr.
Implements newline-delimited JSON (NDJSON) format for machine-parseable logs.

Environment Variables
---------------------
FOUNDRY_AGENTIC_CLI_LOG_LEVEL : str
    Log level control. Supported: DEBUG, INFO, WARNING, ERROR.
    Default: WARNING

Log Record Schema
-----------------
Required fields: ts, level, logger, msg
Optional context fields: op, call_id, attempt, delay_ms, access_decision, http_status

Metadata Separator
------------------
'# ---metadata-start---' precedes metadata JSON on stderr.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# Metadata separator as defined in ADR-005
METADATA_SEPARATOR = "# ---metadata-start---"

# Default log level
DEFAULT_LOG_LEVEL = "WARNING"

# Environment variable for log level
ENV_LOG_LEVEL = "FOUNDRY_AGENTIC_CLI_LOG_LEVEL"

# Supported log levels
SUPPORTED_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class _NdJsonFormatter(logging.Formatter):
    """Newline-delimited JSON formatter for structured logging (ADR-005).

    Each log record is emitted as a single JSON line to stderr.
    Required fields: ts, level, logger, msg
    Optional context fields are merged from extra={} passed to logging calls.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as NDJSON.

        Parameters
        ----------
        record : logging.LogRecord
            The log record to format.

        Returns
        -------
        str
            A single JSON line representing the log record.
        """
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

        log_entry: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Merge optional context fields from extra data
        # These are attached as attributes on the LogRecord by the caller
        optional_fields = [
            "op",
            "call_id",
            "attempt",
            "delay_ms",
            "access_decision",
            "http_status",
            "session_alias",
        ]
        for field in optional_fields:
            value = getattr(record, field, None)
            if value is not None:
                log_entry[field] = value

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exc"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


class LogSetup:
    """Configures Python logging with NDJSON formatter directed to stderr (ADR-005).

    Usage
    -----
    >>> LogSetup.configure(log_level="WARNING")
    >>> logger = logging.getLogger("foundry_cli.common.retry")
    >>> logger.warning("Retrying after 429", extra={"op": "datasets.list", "attempt": 2})
    """

    _configured: bool = False

    @classmethod
    def configure(
        cls,
        log_level: Optional[str] = None,
    ) -> logging.Logger:
        """Configure the root logger with NDJSON formatter to stderr.

        Parameters
        ----------
        log_level : str, optional
            Log level (DEBUG, INFO, WARNING, ERROR). Defaults to env var
            `FOUNDRY_AGENTIC_CLI_LOG_LEVEL`, then to `WARNING`.

        Returns
        -------
        logging.Logger
            The configured root logger.

        Raises
        ------
        ValueError
            If log_level is not one of the supported levels.
        """
        if cls._configured:
            return logging.getLogger()

        # Resolve log level: explicit arg > env var > default
        effective_level = log_level or os.environ.get(ENV_LOG_LEVEL)
        if not effective_level:
            effective_level = DEFAULT_LOG_LEVEL
        effective_level = effective_level.upper()

        if effective_level not in SUPPORTED_LEVELS:
            raise ValueError(
                f"Unsupported log level '{effective_level}'. "
                f"Must be one of: {', '.join(sorted(SUPPORTED_LEVELS))}"
            )

        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, effective_level))

        # Remove any existing handlers to prevent duplicates
        root_logger.handlers.clear()

        # Create stderr handler with NDJSON formatter
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(_NdJsonFormatter())
        root_logger.addHandler(stderr_handler)

        cls._configured = True
        return root_logger

    @classmethod
    def reset(cls) -> None:
        """Reset logging configuration (for testing purposes).

        Clears all handlers and resets the configured flag.
        """
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        cls._configured = False

    @staticmethod
    def emit_metadata_separator() -> None:
        """Emit the metadata separator line to stderr (ADR-005).

        Writes `# ---metadata-start---` to stderr to indicate
        the start of metadata JSON output.
        """
        sys.stderr.write(METADATA_SEPARATOR + "\n")
        sys.stderr.flush()

    @staticmethod
    def emit_metadata(metadata: Dict[str, Any]) -> None:
        """Emit metadata JSON to stderr after the separator.

        Parameters
        ----------
        metadata : Dict[str, Any]
            Metadata dictionary to serialize as JSON on stderr.
        """
        LogSetup.emit_metadata_separator()
        sys.stderr.write(json.dumps(metadata, default=str) + "\n")
        sys.stderr.flush()


def get_logger(name: str) -> logging.Logger:
    """Get a named logger for a Foundry CLI module.

    The logger is automatically configured with NDJSON format to stderr.
    Use `extra={}` on log calls to include optional context fields.

    Parameters
    ----------
    name : str
        Logger name, typically the module path (e.g., 'foundry_cli.common.retry').

    Returns
    -------
    logging.Logger
        A configured logger instance.

    Examples
    --------
    >>> logger = get_logger("foundry_cli.common.retry")
    >>> logger.warning(
    ...     "Retrying after 429: attempt 2/4, delay 1000ms",
    ...     extra={"op": "datasets.dataset.read_table", "attempt": 2, "delay_ms": 1000},
    ... )
    """
    # Ensure logging is configured
    LogSetup.configure()
    return logging.getLogger(name)
