import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pal_found_cli.datasets.scripts import pal_found_datasets_cli


@pytest.fixture(autouse=True)
def restore_default_event_loop():
    yield
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def test_main_runs_async_entrypoint(monkeypatch):
    async def fake_async_main():
        return 23

    monkeypatch.setattr(pal_found_datasets_cli, "async_main", fake_async_main)

    assert pal_found_datasets_cli.main() == 23


def test_async_main_without_resource_returns_user_input_error(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pal-found-datasets"])

    assert (
        asyncio.run(pal_found_datasets_cli.async_main())
        == pal_found_datasets_cli.EXIT_USER_INPUT
    )


def test_transaction_create_requires_transaction_type():
    parser = pal_found_datasets_cli.build_parser()

    with pytest.raises(SystemExit) as error:
        parser.parse_args(["transaction", "create", "ri.foundry.main.dataset.example"])

    assert error.value.code == 2


def test_read_table_requires_format_and_rejects_pagination():
    parser = pal_found_datasets_cli.build_parser()

    with pytest.raises(SystemExit) as missing_format:
        parser.parse_args(["dataset", "read-table", "dataset-rid"])
    assert missing_format.value.code == 2

    with pytest.raises(SystemExit) as unsupported_page_size:
        parser.parse_args(
            [
                "dataset",
                "read-table",
                "dataset-rid",
                "--table-format",
                "CSV",
                "--page-size",
                "10",
            ]
        )
    assert unsupported_page_size.value.code == 2


@pytest.mark.asyncio
async def test_transaction_create_passes_type_and_branch_to_sdk():
    args = pal_found_datasets_cli.build_parser().parse_args(
        [
            "transaction",
            "create",
            "ri.foundry.main.dataset.example",
            "--transaction-type",
            "APPEND",
            "--branch-name",
            "updates",
        ]
    )
    client = AsyncMock()
    client.create.return_value = {"rid": "ri.foundry.main.transaction.example"}

    result = await pal_found_datasets_cli._invoke(
        "transaction", "create", client, args, timeout=30
    )

    assert result == {"rid": "ri.foundry.main.transaction.example"}
    client.create.assert_awaited_once_with(
        dataset_rid="ri.foundry.main.dataset.example",
        transaction_type="APPEND",
        branch_name="updates",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_jobs_pagination_decodes_raw_pages_and_preserves_cursor():
    args = pal_found_datasets_cli.build_parser().parse_args(
        ["dataset", "jobs", "dataset-rid"]
    )
    pages = [
        SimpleNamespace(
            decode=lambda: SimpleNamespace(
                data=[{"rid": "job-1"}], next_page_token="next"
            )
        ),
        SimpleNamespace(
            decode=lambda: SimpleNamespace(
                data=[{"rid": "job-2"}], next_page_token=None
            )
        ),
    ]
    jobs = AsyncMock(side_effect=pages)
    client = SimpleNamespace(with_raw_response=SimpleNamespace(jobs=jobs))
    helper = pal_found_datasets_cli.PaginationHelper(page_size=2, batch_pages=2)

    result = await pal_found_datasets_cli._invoke_paginated(
        "dataset", "jobs", client, args, 30, helper
    )

    assert result == [{"rid": "job-1"}, {"rid": "job-2"}]
    assert helper.pages_fetched == 2
    assert helper.next_page_token is None
    assert jobs.await_args_list[0].kwargs["page_token"] is None
    assert jobs.await_args_list[1].kwargs["page_token"] == "next"


@pytest.mark.asyncio
async def test_file_list_pagination_uses_raw_response_data():
    args = pal_found_datasets_cli.build_parser().parse_args(
        ["file", "list", "dataset-rid", "--path-prefix", "reports/"]
    )
    response = SimpleNamespace(
        decode=lambda: SimpleNamespace(
            data=[{"path": "reports/q3.csv"}], next_page_token="more"
        )
    )
    list_page = AsyncMock(return_value=response)
    client = SimpleNamespace(with_raw_response=SimpleNamespace(list=list_page))
    helper = pal_found_datasets_cli.PaginationHelper(page_size=10)

    result = await pal_found_datasets_cli._invoke_paginated(
        "file", "list", client, args, 30, helper
    )

    assert result == [{"path": "reports/q3.csv"}]
    assert helper.next_page_token == "more"
    assert list_page.await_args.kwargs["path_prefix"] == "reports/"


@pytest.mark.asyncio
async def test_file_content_streams_bounded_download(monkeypatch, capsys):
    async def chunks():
        yield b"abc"
        yield b"def"

    response = SimpleNamespace(aiter_bytes=chunks)

    @asynccontextmanager
    async def stream_response():
        yield response

    content = Mock(return_value=stream_response())
    client = SimpleNamespace(with_streaming_response=SimpleNamespace(content=content))
    temp_root = Path(__file__).resolve().parents[2] / ".ept" / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=temp_root) as download_root:
        cfg = SimpleNamespace(
            load=lambda: None,
            log_level="ERROR",
            timeout_s=30,
            download_path=download_root,
            max_download_bytes=4,
        )
        monkeypatch.setattr(pal_found_datasets_cli, "ConfigLoader", lambda: cfg)
        monkeypatch.setattr(
            pal_found_datasets_cli,
            "AccessControlGuard",
            lambda *_: SimpleNamespace(check=lambda *_: None),
        )
        monkeypatch.setattr(pal_found_datasets_cli, "_get_client", lambda *_: client)
        monkeypatch.setattr(
            pal_found_datasets_cli.LogSetup, "configure", lambda **_: None
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "pal-found-datasets",
                "file",
                "content",
                "ri.foundry.main.dataset.example",
                "--file-path",
                "reports/summary.bin",
                "--output",
                "summary.bin",
                "--format",
                "json",
            ],
        )

        assert await pal_found_datasets_cli.async_main() == 0
        result = json.loads(capsys.readouterr().out)
        assert Path(result["file_path"]).read_bytes() == b"abcd"
        assert result["file_size"] == 4
        assert result["truncated"] is True
        assert result["source_size_at_least"] == 5
        assert result["checksum_sha256"] == hashlib.sha256(b"abcd").hexdigest()

    content.assert_called_once_with(
        dataset_rid="ri.foundry.main.dataset.example",
        file_path="reports/summary.bin",
        branch_name=None,
        end_transaction_rid=None,
        start_transaction_rid=None,
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_read_table_forwards_selectors_and_saves_csv():
    args = pal_found_datasets_cli.build_parser().parse_args(
        [
            "dataset",
            "read-table",
            "dataset-rid",
            "--table-format",
            "CSV",
            "--columns-json",
            '["id","name"]',
            "--row-limit",
            "2",
            "--branch-name",
            "updates",
            "--start-transaction-rid",
            "start-rid",
            "--end-transaction-rid",
            "end-rid",
            "--output",
            "rows.csv",
        ]
    )

    async def chunks():
        yield b"id,name\n1,Ada\n"

    @asynccontextmanager
    async def stream_response():
        yield SimpleNamespace(aiter_bytes=chunks)

    read_table = Mock(return_value=stream_response())
    client = SimpleNamespace(
        with_streaming_response=SimpleNamespace(read_table=read_table)
    )
    temp_root = Path(__file__).resolve().parents[2] / ".ept" / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=temp_root) as download_root:
        cfg = SimpleNamespace(download_path=download_root, max_download_bytes=100)
        result = await pal_found_datasets_cli._download_read_table(
            client, args, 12, cfg
        )
        assert Path(result["file_path"]).read_bytes() == b"id,name\n1,Ada\n"
        assert result["file_size"] == 14
        assert result["truncated"] is False
        assert result["mime_type"] == "text/csv"

    read_table.assert_called_once_with(
        dataset_rid="dataset-rid",
        format="CSV",
        branch_name="updates",
        columns=["id", "name"],
        end_transaction_rid="end-rid",
        row_limit=2,
        start_transaction_rid="start-rid",
        request_timeout=12,
    )
