#!/usr/bin/env python3
"""OutputFormatter with auto-selection (ADR-004).

Implements the format auto-selection algorithm defined in ADR-004.
Supports JSON and TOON output formats with intelligent format selection
based on data shape and user configuration.

Format Selection Algorithm (ADR-004)
------------------------------------
1. Explicit format always wins
2. Errors always use JSON
3. Non-list top-level always uses JSON
4. Empty list uses JSON
5. Extract field sets from all items
6. Any non-dict item → JSON
7. All items must share identical field set → TOON, else JSON
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional

# Environment variable for default format
ENV_DEFAULT_FORMAT = "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT"


class OutputFormatter:
    """Formats output data as JSON or TOON with auto-selection (ADR-004).

    Implements the 7-step auto-selection algorithm from ADR-004.
    Supports explicit format override and pretty-printing.

    Parameters
    ----------
    format_setting : str, optional
        Format setting: 'json', 'toon', or 'auto'. Defaults to env var
        `FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT`, then to 'auto'.
    pretty : bool, optional
        Enable pretty-printing for JSON output. Default: False.

    Examples
    --------
    >>> formatter = OutputFormatter(format_setting="auto")
    >>> output = formatter.format(data)
    >>> print(output)
    """

    def __init__(
        self,
        format_setting: Optional[str] = None,
        pretty: bool = False,
    ) -> None:
        self.format_setting = format_setting or os.environ.get(ENV_DEFAULT_FORMAT, "auto")
        self.pretty = pretty

    def _select_format(self, data: Any) -> str:
        """Apply ADR-004 format auto-selection algorithm.

        Algorithm:
        1. Explicit format always wins
        2. Errors always use JSON
        3. Non-list top-level always uses JSON
        4. Empty list uses JSON
        5. Extract field sets from all items
        6. Any non-dict item → JSON
        7. All items must share identical field set → TOON, else JSON

        Parameters
        ----------
        data : Any
            The data to format.

        Returns
        -------
        str
            Selected format: 'json' or 'toon'.
        """
        # 1. Explicit format always wins
        if self.format_setting in ("json", "toon"):
            return self.format_setting

        # 2. Errors always use JSON
        if isinstance(data, dict) and data.get("error"):
            return "json"

        # 3. Non-list top-level always uses JSON
        if not isinstance(data, list):
            return "json"

        # 4. Empty list uses JSON
        if len(data) == 0:
            return "json"

        # 5. Extract field sets from all items
        field_sets = []
        for item in data:
            if isinstance(item, dict):
                field_sets.append(frozenset(item.keys()))

        # 6. Any non-dict item → JSON
        if len(field_sets) != len(data):
            return "json"

        # 7. All items must share identical field set → TOON, else JSON
        if len(set(field_sets)) == 1:
            return "toon"

        return "json"

    def _format_json(self, data: Any) -> str:
        """Format data as JSON.

        Parameters
        ----------
        data : Any
            Data to serialize as JSON.

        Returns
        -------
        str
            JSON string.
        """
        if self.pretty:
            return json.dumps(data, indent=2, default=str)
        return json.dumps(data, default=str)

    def _format_toon(self, data: List[Dict[str, Any]]) -> str:
        """Format data as TOON (tabular output).

        TOON format: human-readable table/line format.
        Each dict item becomes a row with aligned columns.

        Parameters
        ----------
        data : List[Dict[str, Any]]
            List of uniform dicts to format as table.

        Returns
        -------
        str
            TOON-formatted string.
        """
        if not data:
            return ""

        # Extract headers (keys from first item)
        headers = list(data[0].keys())

        # Calculate column widths
        col_widths = {h: len(str(h)) for h in headers}
        for row in data:
            for h in headers:
                val_len = len(str(row.get(h, "")))
                if val_len > col_widths[h]:
                    col_widths[h] = val_len

        # Build table
        lines = []

        # Header line
        header_line = "  ".join(str(h).ljust(col_widths[h]) for h in headers)
        lines.append(header_line)

        # Separator
        separator = "  ".join("-" * col_widths[h] for h in headers)
        lines.append(separator)

        # Data rows
        for row in data:
            row_line = "  ".join(
                str(row.get(h, "")).ljust(col_widths[h]) for h in headers
            )
            lines.append(row_line)

        return "\n".join(lines)

    def format(self, data: Any) -> str:
        """Format data according to format setting and ADR-004 algorithm.

        Parameters
        ----------
        data : Any
            Data to format. Can be dict, list, or scalar.

        Returns
        -------
        str
            Formatted string output.

        Raises
        ------
        ValueError
            If format_setting is not one of 'json', 'toon', 'auto'.
        """
        if self.format_setting not in ("json", "toon", "auto"):
            raise ValueError(
                f"Invalid format_setting '{self.format_setting}'. "
                f"Must be one of: 'json', 'toon', 'auto'"
            )

        selected_format = self._select_format(data)

        if selected_format == "toon":
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return self._format_toon(data)
            # Fallback to JSON if TOON not applicable
            return self._format_json(data)

        return self._format_json(data)

    def format_error(self, error_data: Dict[str, Any]) -> str:
        """Format error data (always JSON per ADR-004).

        Parameters
        ----------
        error_data : Dict[str, Any]
            Error envelope dictionary.

        Returns
        -------
        str
            JSON-formatted error string.
        """
        return self._format_json(error_data)

    def emit(self, data: Any) -> None:
        """Format and emit data to stdout.

        Parameters
        ----------
        data : Any
            Data to format and emit.
        """
        output = self.format(data)
        sys.stdout.write(output + "\n")
        sys.stdout.flush()

    def emit_error(self, error_data: Dict[str, Any]) -> None:
        """Format and emit error data to stderr.

        Per ADR-004, error output always goes to stderr (not stdout),
        keeping the CLI contract of stdout for primary result data only.

        Parameters
        ----------
        error_data : Dict[str, Any]
            Error envelope dictionary.
        """
        output = self.format_error(error_data)
        sys.stderr.write(output + "\n")
        sys.stderr.flush()

    def emit_to_stderr(self, data: Any) -> None:
        """Emit data (e.g. pagination metadata) to stderr as JSON.

        Per ADR-004, pagination metadata always goes to stderr as JSON.

        Parameters
        ----------
        data : Any
            Data to emit to stderr.
        """
        output = json.dumps(data, default=str)
        sys.stderr.write(output + "\n")
        sys.stderr.flush()
