#!/usr/bin/env python3
"""PaginationHelper — manages --page-size, --page-token, --batch-pages (SRS §3.5).

Handles paginated SDK responses and emits metadata to stderr
with pagination cursors per ADR-005.

Default: return first page only with page_token on stderr.
Batch mode: agent specifies number of pages (max 40).

Environment variables:
- FOUNDRY_AGENTIC_CLI_DEFAULT_PAGE_SIZE (default: 100)
- FOUNDRY_AGENTIC_CLI_MAX_BATCH_PAGES (default: 40)
"""

import json
import os
import sys
from typing import Any

from pal_found_cli.common.log_setup import METADATA_SEPARATOR

# Default values (from SRS-001 §5)
DEFAULT_PAGE_SIZE = int(os.environ.get("FOUNDRY_AGENTIC_CLI_DEFAULT_PAGE_SIZE", "100"))
HARD_MAX_BATCH_PAGES = 40
MAX_BATCH_PAGES = min(
    int(
        os.environ.get("FOUNDRY_AGENTIC_CLI_MAX_BATCH_PAGES", str(HARD_MAX_BATCH_PAGES))
    ),
    HARD_MAX_BATCH_PAGES,
)


class PaginationHelper:
    """Manages pagination for SDK calls.

    Parameters
    ----------
    page_size : int or None
        Number of items per page. Defaults to FOUNDRY_AGENTIC_CLI_DEFAULT_PAGE_SIZE (100).
    page_token : str or None
        Resume from this cursor.
    batch_pages : int or None
        Number of pages to retrieve (default 1, max FOUNDRY_AGENTIC_CLI_MAX_BATCH_PAGES).

    Usage
    -----
    >>> helper = PaginationHelper(page_size=50, page_token=None, batch_pages=1)
    >>> result = await helper.paginate(client_method, helper)
    >>> helper.emit_metadata()  # writes to stderr
    """

    def __init__(
        self,
        page_size: int | None = None,
        page_token: str | None = None,
        batch_pages: int | None = None,
    ) -> None:
        self.page_size = self._validate_positive_int(
            "page_size",
            page_size if page_size is not None else DEFAULT_PAGE_SIZE,
        )
        self.page_token = page_token
        raw_batch = self._validate_positive_int(
            "batch_pages",
            batch_pages if batch_pages is not None else 1,
        )
        self.batch_pages = min(raw_batch, MAX_BATCH_PAGES)
        self._next_page_token: str | None = None
        self._total_items: int = 0
        self._pages_fetched: int = 0

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> int:
        """Validate that a pagination argument is a positive integer."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be a positive integer")
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @property
    def next_page_token(self) -> str | None:
        """Cursor for next page."""
        return self._next_page_token

    @property
    def total_items(self) -> int:
        """Total items fetched across all pages."""
        return self._total_items

    @property
    def pages_fetched(self) -> int:
        """Number of pages fetched."""
        return self._pages_fetched

    def get_sdk_params(self) -> dict[str, Any]:
        """Get pagination parameters for SDK call.

        Returns
        -------
        dict
            SDK-compatible pagination kwargs.
        """
        params: dict[str, Any] = {}
        if self.page_size is not None:
            params["page_size"] = self.page_size
        if self.page_token is not None:
            params["page_token"] = self.page_token
        return params

    def _extract_items(self, response: Any) -> list[Any]:
        """Extract items list from SDK response.

        Parameters
        ----------
        response : Any
            SDK response object.

        Returns
        -------
        list
            List of items from the response.
        """
        if isinstance(response, dict):
            return response.get("items", []) or []
        if isinstance(response, list):
            return response
        if hasattr(response, "items"):
            return getattr(response, "items", []) or []
        return [response] if response else []

    def _extract_next_token(self, response: Any) -> str | None:
        """Extract next page token from SDK response.

        Parameters
        ----------
        response : Any
            SDK response object.

        Returns
        -------
        str or None
            Next page token.
        """
        if isinstance(response, dict):
            return response.get("next_page_token") or response.get("nextPageToken")
        if hasattr(response, "next_page_token"):
            return getattr(response, "next_page_token", None)
        if hasattr(response, "nextPageToken"):
            return getattr(response, "nextPageToken", None)
        return None

    async def paginate(
        self,
        call_func,
        **call_kwargs,
    ) -> list[Any]:
        """Execute paginated SDK calls.

        Parameters
        ----------
        call_func : callable
            Async SDK method to call.
        **call_kwargs
            Additional kwargs to pass to SDK method.

        Returns
        -------
        list
            Aggregated items from all pages.
        """
        all_items: list[Any] = []
        token = self.page_token

        for _ in range(self.batch_pages):
            # Build params
            params = dict(call_kwargs)
            if self.page_size is not None:
                params["page_size"] = self.page_size
            if token is not None:
                params["page_token"] = token

            response = await call_func(**params)
            items = self._extract_items(response)
            all_items.extend(items)
            self._total_items += len(items)
            self._pages_fetched += 1

            token = self._extract_next_token(response)
            if token is None:
                break

        self._next_page_token = token
        return all_items

    def emit_metadata(self) -> None:
        """Emit pagination metadata to stderr (ADR-005).

        Writes METADATA_SEPARATOR followed by JSON metadata.
        """
        metadata: dict[str, Any] = {
            "pages_fetched": self._pages_fetched,
            "total_items": self._total_items,
        }
        if self._next_page_token:
            metadata["next_page_token"] = self._next_page_token
        if self.page_size:
            metadata["page_size"] = self.page_size

        sys.stderr.write(METADATA_SEPARATOR + "\n")
        sys.stderr.write(json.dumps(metadata) + "\n")
        sys.stderr.flush()
