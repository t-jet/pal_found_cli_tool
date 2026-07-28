"""Focused tests for bounded, atomic binary downloads."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from foundry_cli.common.binary_download_handler import (
    BinaryDownloadHandler,
    DownloadError,
    InvalidDownloadError,
)


class ChunkStream:
    """Async byte stream that records consumption and closure."""

    def __init__(self, chunks: list[bytes], error_after: int | None = None) -> None:
        self.chunks = chunks
        self.error_after = error_after
        self.yielded = 0
        self.closed = False

    def __aiter__(self) -> ChunkStream:
        return self

    async def __anext__(self) -> bytes:
        if self.error_after is not None and self.yielded == self.error_after:
            raise OSError("stream failed")
        if self.yielded == len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.yielded]
        self.yielded += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True


async def _save(
    handler: BinaryDownloadHandler,
    stream: ChunkStream,
    **overrides: str | None,
):
    values = {
        "original_filename": "report.bin",
        "namespace": "datasets",
        "operation": "download",
        "content_length": None,
        "content_encoding": None,
        "mime_type": "application/octet-stream",
    }
    values.update(overrides)
    return await handler.save(stream, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_truncated", "expected_source"),
    [
        (b"abc", False, 3),
        (b"12345", False, 5),
    ],
)
async def test_unknown_length_reaches_eof_without_false_truncation(
    tmp_path: Path,
    payload: bytes,
    expected_truncated: bool,
    expected_source: int,
) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=5)
    stream = ChunkStream([payload])

    result = await _save(handler, stream)

    path = Path(result.file_path)
    assert path.read_bytes() == payload
    assert path.parent.parent == tmp_path.resolve()
    assert result.file_size == len(payload)
    assert result.truncated is expected_truncated
    assert result.source_size == expected_source
    assert result.source_size_at_least is None
    assert (
        result.checksum_md5 == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    )
    assert result.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    assert stream.closed is True


@pytest.mark.asyncio
async def test_unknown_length_reads_only_limit_plus_one_and_hashes_stored_prefix(
    tmp_path: Path,
) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=5)
    stream = ChunkStream([b"123", b"456789", b"must-not-be-read"])

    result = await _save(handler, stream)

    assert Path(result.file_path).read_bytes() == b"12345"
    assert result.truncated is True
    assert result.source_size is None
    assert result.source_size_at_least == 6
    assert result.checksum_sha256 == hashlib.sha256(b"12345").hexdigest()
    assert stream.yielded == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_known_oversize_stops_after_prefix_and_keeps_declared_size(
    tmp_path: Path,
) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=4)
    stream = ChunkStream([b"abcdef", b"must-not-be-read"])

    result = await _save(handler, stream, content_length="99")

    assert Path(result.file_path).read_bytes() == b"abcd"
    assert result.file_size == 4
    assert result.truncated is True
    assert result.source_size == 99
    assert result.source_size_at_least is None
    assert stream.yielded == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_length", "content_encoding"),
    [("not-a-number", None), ("-1", None), ("99", "gzip")],
)
async def test_inapplicable_content_length_uses_observed_size(
    tmp_path: Path,
    content_length: str,
    content_encoding: str | None,
) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=10)

    result = await _save(
        handler,
        ChunkStream([b"data"]),
        content_length=content_length,
        content_encoding=content_encoding,
    )

    assert result.source_size == 4
    assert result.truncated is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    ["../secret", r"..\secret", "/absolute", "nul\x00name", ".", ".."],
)
async def test_unsafe_filename_is_rejected_before_root_creation(
    tmp_path: Path,
    filename: str,
) -> None:
    root = tmp_path / "downloads"
    handler = BinaryDownloadHandler(root, max_download_bytes=10)

    with pytest.raises(InvalidDownloadError):
        await _save(handler, ChunkStream([b"secret"]), original_filename=filename)

    assert root.exists() is False


@pytest.mark.asyncio
async def test_invalid_limit_fails_before_stream_or_filesystem_use(
    tmp_path: Path,
) -> None:
    root = tmp_path / "downloads"
    stream = ChunkStream([b"data"])

    with pytest.raises(InvalidDownloadError):
        await _save(BinaryDownloadHandler(root, max_download_bytes=0), stream)

    assert stream.yielded == 0
    assert stream.closed is False
    assert root.exists() is False


@pytest.mark.asyncio
async def test_stream_failure_removes_partial_and_temporary_files(
    tmp_path: Path,
) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=20)
    stream = ChunkStream([b"partial"], error_after=1)

    with pytest.raises(OSError, match="stream failed"):
        await _save(handler, stream)

    assert list(tmp_path.rglob("*")) == []
    assert stream.closed is True


@pytest.mark.asyncio
async def test_non_bytes_chunk_is_rejected_and_cleaned(tmp_path: Path) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=20)
    stream = ChunkStream([b"ok", "bad"])  # type: ignore[list-item]

    with pytest.raises(DownloadError, match="non-bytes"):
        await _save(handler, stream)

    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_concurrent_same_name_downloads_never_overwrite(tmp_path: Path) -> None:
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=20)

    first, second = await asyncio.gather(
        _save(handler, ChunkStream([b"first"])),
        _save(handler, ChunkStream([b"second"])),
    )

    first_path = Path(first.file_path)
    second_path = Path(second.file_path)
    assert first_path != second_path
    assert first_path.parent != second_path.parent
    assert {first_path.read_bytes(), second_path.read_bytes()} == {b"first", b"second"}


@pytest.mark.asyncio
async def test_download_dir_permissions_are_umask_independent_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEREVIEW-005 D2: per-download UUID dir is restricted (0o700) via chmod.

    Path.mkdir applies the kernel umask to its mode argument, so without a
    follow-up chmod the directory could end up more open than 0o700. The
    handler calls _restrict_directory on the download dir after mkdir, which
    performs the chmod. This test records every chmod call so we assert the
    UUID directory receives a direct chmod to 0o700 regardless of umask.
    """
    handler = BinaryDownloadHandler(tmp_path, max_download_bytes=10)

    real_chmod = os.chmod
    chmod_calls: list[tuple[str, int]] = []

    def recording_chmod(path, mode):  # type: ignore[no-untyped-def]
        chmod_calls.append((str(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    monkeypatch.setattr(
        "foundry_cli.common.binary_download_handler.os.chmod", recording_chmod
    )

    await _save(handler, ChunkStream([b"payload"]))

    # On POSIX the per-download directory must be explicitly chmod'd 0o700.
    # On Windows _restrict_directory is a no-op, so only assert on POSIX.
    if os.name != "nt":
        uuid_parents = {str(Path(p).resolve()) for p, _ in chmod_calls}
        download_dirs = {
            p
            for p in uuid_parents
            if tmp_path.resolve() == Path(p).parent.resolve()
            and Path(p).name != tmp_path.name
        }
        assert download_dirs, "expected a chmod on the UUID download directory"
        for p, mode in chmod_calls:
            if Path(p).resolve() in download_dirs:
                assert mode == 0o700, (
                    f"download dir {p} chmod mode {oct(mode)} != 0o700"
                )
                # Directory must actually be owner-only on disk.
                actual = stat.S_IMODE(Path(p).stat().st_mode)
                assert actual == 0o700, (
                    f"download dir {p} on-disk mode {oct(actual)} != 0o700"
                )
