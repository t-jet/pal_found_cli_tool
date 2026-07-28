#!/usr/bin/env python3
"""Unit tests for PaginationHelper (DEV-004 — dict-extraction fix + batch behaviour).

Covers the regression fixed under DEV-004:
- _extract_items / _extract_next_token must not confuse a dict's built-in
  ``items()`` method with its ``"items"`` key (the dict branch must run
  before the ``hasattr`` branch).
- Batch aggregation respects --batch-pages, the max-batch cap, and propagates
  SDK page tokens; null/no-next-token is reported correctly.
- emit_metadata writes the ADR-005 separator block to stderr.

Framework: pytest with pytest-asyncio.
Run: pytest tests/test_pagination_helper.py -v --tb=long
"""

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure src is on path
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.pagination_helper import (  # noqa: E402
    MAX_BATCH_PAGES,
    PaginationHelper,
)


# ===========================================================================
# Dict extraction regression (DEV-004 bug #2)
# ===========================================================================

class TestExtractItemsDict:
    """_extract_items must read a dict's "items" key, not its .items() method."""

    def test_dict_with_items_key(self):
        """A dict with an 'items' key returns that key's value."""
        helper = PaginationHelper()
        resp = {"items": [{"a": 1}], "next_page_token": "tok"}
        assert helper._extract_items(resp) == [{"a": 1}]

    def test_dict_without_items_key(self):
        """A dict without an 'items' key returns an empty list."""
        helper = PaginationHelper()
        resp = {"data": [1, 2], "nextPageToken": "tok"}
        assert helper._extract_items(resp) == []

    def test_dict_empty_items_value(self):
        """A dict whose 'items' value is falsy returns an empty list."""
        helper = PaginationHelper()
        assert helper._extract_items({"items": None}) == []
        assert helper._extract_items({"items": []}) == []


class TestExtractNextTokenDict:
    """_extract_next_token must read dict keys, not attributes."""

    def test_dict_next_page_token(self):
        helper = PaginationHelper()
        assert helper._extract_next_token({"items": [], "next_page_token": "tok"}) == "tok"

    def test_dict_next_page_token_camel(self):
        helper = PaginationHelper()
        assert helper._extract_next_token({"nextPageToken": "ctok"}) == "ctok"

    def test_dict_no_token(self):
        helper = PaginationHelper()
        assert helper._extract_next_token({"items": [1]}) is None


# ===========================================================================
# Object (Pydantic-style) extraction still works
# ===========================================================================

class TestExtractItemsObject:
    """_extract_items falls back to attribute access for non-dict responses."""

    def test_object_with_items_attr(self):
        helper = PaginationHelper()
        obj = MagicMock()
        obj.items = [{"x": 1}]
        assert helper._extract_items(obj) == [{"x": 1}]

    def test_list_response(self):
        helper = PaginationHelper()
        assert helper._extract_items([1, 2, 3]) == [1, 2, 3]


# ===========================================================================
# Batch aggregation (FR-PAG-4, FR-PAG-5)
# ===========================================================================

class TestBatchAggregation:
    """paginate aggregates across pages up to batch_pages."""

    @pytest.mark.asyncio
    async def test_single_page_default(self):
        """Default (batch_pages=1) fetches exactly one page."""
        calls = []

        async def fake(**kw):
            calls.append(kw)
            return {"items": [{"i": 1}], "next_page_token": "tok1"}

        helper = PaginationHelper(page_size=10)
        items = await helper.paginate(fake)
        assert items == [{"i": 1}]
        assert helper.pages_fetched == 1
        assert helper.next_page_token == "tok1"

    @pytest.mark.asyncio
    async def test_multi_page_aggregation(self):
        """batch_pages=3 aggregates three pages of items."""

        async def fake(**kw):
            n = kw.get("page_token")
            page = int(n[-1]) if n else 0
            nxt = f"tok{page + 1}"
            return {"items": [{"page": page}], "next_page_token": nxt}

        helper = PaginationHelper(page_size=10, batch_pages=3)
        items = await helper.paginate(fake)
        assert [it["page"] for it in items] == [0, 1, 2]
        assert helper.pages_fetched == 3
        assert helper.next_page_token == "tok3"  # cursor for a 4th page

    @pytest.mark.asyncio
    async def test_stops_when_no_next_token(self):
        """Iteration stops as soon as the SDK returns no next token."""

        async def fake(**kw):
            return {"items": [{"i": 1}], "next_page_token": None}

        helper = PaginationHelper(page_size=10, batch_pages=5)
        items = await helper.paginate(fake)
        assert items == [{"i": 1}]
        assert helper.pages_fetched == 1

    def test_max_batch_cap_enforced(self):
        """batch_pages above MAX_BATCH_PAGES is clamped to the cap."""
        helper = PaginationHelper(batch_pages=1000)
        assert helper.batch_pages == MAX_BATCH_PAGES

    def test_env_max_batch_cannot_raise_hard_cap(self, monkeypatch):
        """Environment override cannot raise the SRS hard cap above 40."""
        import foundry_cli.common.pagination_helper as pagination_module

        monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_MAX_BATCH_PAGES", "100")
        reloaded = importlib.reload(pagination_module)
        try:
            assert reloaded.MAX_BATCH_PAGES == 40
            assert reloaded.PaginationHelper(batch_pages=100).batch_pages == 40
        finally:
            monkeypatch.delenv("FOUNDRY_AGENTIC_CLI_MAX_BATCH_PAGES", raising=False)
            importlib.reload(pagination_module)

    @pytest.mark.asyncio
    async def test_batch_cap_limits_sdk_calls(self):
        """batch_pages above cap fetches no more than MAX_BATCH_PAGES pages."""
        calls = []

        async def fake(**kw):
            calls.append(kw)
            return {"items": [len(calls)], "next_page_token": f"tok{len(calls)}"}

        helper = PaginationHelper(page_size=1, batch_pages=MAX_BATCH_PAGES + 10)
        items = await helper.paginate(fake)
        assert len(calls) == MAX_BATCH_PAGES
        assert len(items) == MAX_BATCH_PAGES
        assert helper.next_page_token == f"tok{MAX_BATCH_PAGES}"


class TestPageSizeValidation:
    """PaginationHelper rejects invalid page sizes."""

    @pytest.mark.parametrize("page_size", [0, -1])
    def test_zero_or_negative_page_size_rejected(self, page_size):
        with pytest.raises(ValueError, match="page_size"):
            PaginationHelper(page_size=page_size)

    @pytest.mark.parametrize("page_size", ["10", 1.5])
    def test_non_integer_page_size_rejected(self, page_size):
        with pytest.raises((TypeError, ValueError), match="page_size"):
            PaginationHelper(page_size=page_size)

    @pytest.mark.parametrize("batch_pages", [0, -1])
    def test_zero_or_negative_batch_pages_rejected(self, batch_pages):
        with pytest.raises(ValueError, match="batch_pages"):
            PaginationHelper(batch_pages=batch_pages)


# ===========================================================================
# SDK param propagation (FR-PAG-3)
# ===========================================================================

class TestSdkParams:
    """get_sdk_params forwards page_size and page_token."""

    def test_missing_token_omitted(self):
        helper = PaginationHelper(page_size=50, page_token=None)
        params = helper.get_sdk_params()
        assert params == {"page_size": 50}

    def test_both_forwarded(self):
        helper = PaginationHelper(page_size=25, page_token="abc")
        params = helper.get_sdk_params()
        assert params == {"page_size": 25, "page_token": "abc"}

    @pytest.mark.asyncio
    async def test_paginate_propagates_sdk_page_tokens(self):
        calls = []

        async def fake(**kw):
            calls.append(kw)
            token = kw.get("page_token")
            if token is None:
                return {"items": ["first"], "next_page_token": "tok1"}
            if token == "tok1":
                return {"items": ["second"], "next_page_token": "tok2"}
            return {"items": ["third"], "next_page_token": None}

        helper = PaginationHelper(page_size=25, batch_pages=3)
        assert await helper.paginate(fake, dataset_rid="rid") == ["first", "second", "third"]
        assert calls == [
            {"dataset_rid": "rid", "page_size": 25},
            {"dataset_rid": "rid", "page_size": 25, "page_token": "tok1"},
            {"dataset_rid": "rid", "page_size": 25, "page_token": "tok2"},
        ]

    @pytest.mark.asyncio
    async def test_initial_page_token_propagated_to_first_sdk_call(self):
        calls = []

        async def fake(**kw):
            calls.append(kw)
            return {"items": [], "next_page_token": None}

        helper = PaginationHelper(page_size=10, page_token="start", batch_pages=1)
        await helper.paginate(fake)
        assert calls == [{"page_size": 10, "page_token": "start"}]


# ===========================================================================
# emit_metadata to stderr (ADR-005, FR-PAG-2)
# ===========================================================================

class TestEmitMetadata:
    """emit_metadata writes the separator + JSON metadata to stderr."""

    def test_writes_separator_and_json(self, capsys):
        helper = PaginationHelper(page_size=50)
        helper._pages_fetched = 2
        helper._total_items = 17
        helper._next_page_token = "cursor42"
        helper.emit_metadata()
        captured = capsys.readouterr()
        lines = captured.err.strip().splitlines()
        # First line is the ADR-005 metadata separator.
        from foundry_cli.common.log_setup import METADATA_SEPARATOR
        assert lines[0] == METADATA_SEPARATOR
        payload = json.loads(lines[1])
        assert payload["pages_fetched"] == 2
        assert payload["total_items"] == 17
        assert payload["next_page_token"] == "cursor42"
        assert payload["page_size"] == 50

    def test_no_next_token_omitted(self, capsys):
        helper = PaginationHelper(page_size=10)
        helper._pages_fetched = 1
        helper._total_items = 3
        helper._next_page_token = None
        helper.emit_metadata()
        captured = capsys.readouterr()
        lines = captured.err.strip().splitlines()
        payload = json.loads(lines[1])
        assert "next_page_token" not in payload

    def test_null_no_more_pages_metadata_has_counts_and_separator(self, capsys):
        helper = PaginationHelper(page_size=10)
        helper._pages_fetched = 1
        helper._total_items = 0
        helper._next_page_token = None
        helper.emit_metadata()
        captured = capsys.readouterr()
        lines = captured.err.strip().splitlines()
        from foundry_cli.common.log_setup import METADATA_SEPARATOR
        assert lines[0] == METADATA_SEPARATOR
        assert json.loads(lines[1]) == {
            "pages_fetched": 1,
            "total_items": 0,
            "page_size": 10,
        }
