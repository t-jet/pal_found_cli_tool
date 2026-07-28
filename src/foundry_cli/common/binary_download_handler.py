"""Bounded, atomic binary download persistence (DESIGN-005)."""

from __future__ import annotations

import hashlib
import inspect
import logging
import mimetypes
import os
import re
import tempfile
import unicodedata
import uuid
from collections.abc import AsyncIterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundry_cli.common.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_DOWNLOAD_ROOT = Path(".foundry-data/downloads")
_DEFAULT_MAX_DOWNLOAD_BYTES = 1_572_864


class DownloadError(Exception):
    """Base error for binary download persistence."""

    exit_code = 6


class InvalidDownloadError(DownloadError, ValueError):
    """Raised when download configuration or a filename is invalid."""

    exit_code = 1


@dataclass(frozen=True)
class DownloadResult:
    """Metadata for a downloaded file prefix."""

    file_path: str
    file_size: int
    checksum_md5: str
    checksum_sha256: str
    mime_type: str | None
    truncated: bool
    source_size: int | None
    source_size_at_least: int | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable result envelope."""
        return asdict(self)


class BinaryDownloadHandler:
    """Stream binary content into a contained UUID directory.

    The handler reads at most ``max_download_bytes + 1`` bytes when source
    length is unknown and publishes the stored prefix atomically.
    """

    def __init__(
        self,
        download_root: str | Path | None = None,
        max_download_bytes: int | None = None,
        *,
        config: ConfigLoader | None = None,
    ) -> None:
        cfg = config or ConfigLoader()
        self.download_root = Path(
            download_root
            if download_root is not None
            else getattr(cfg, "download_path", _DEFAULT_DOWNLOAD_ROOT)
        ).expanduser()
        self.max_download_bytes = (
            max_download_bytes
            if max_download_bytes is not None
            else getattr(cfg, "max_download_bytes", _DEFAULT_MAX_DOWNLOAD_BYTES)
        )

    async def save(
        self,
        chunks: AsyncIterable[bytes],
        *,
        original_filename: str | None,
        namespace: str,
        operation: str,
        content_length: str | None = None,
        content_encoding: str | None = None,
        mime_type: str | None = None,
    ) -> DownloadResult:
        """Store a bounded stream and return checksums for persisted bytes."""
        limit = self.max_download_bytes
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise InvalidDownloadError("max_download_bytes must be a positive integer")

        filename = self._safe_filename(
            original_filename,
            namespace=namespace,
            operation=operation,
            mime_type=mime_type,
        )
        root = self.download_root.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._restrict_directory(root)

        download_dir = root / str(uuid.uuid4())
        download_dir.mkdir(mode=0o700)
        # Path.mkdir applies the kernel umask to the supplied mode. Calling
        # _restrict_directory invokes os.chmod directly, bypassing umask so
        # the per-download UUID directory is owner-only (0o700) on POSIX,
        # consistent with the protection applied to ``root`` above.
        self._restrict_directory(download_dir)
        final_path = (download_dir / filename).resolve()
        if final_path.parent != download_dir.resolve() or not final_path.is_relative_to(root):
            self._remove_empty_directory(download_dir)
            raise InvalidDownloadError("Download filename escapes configured path")

        known_size = self._applicable_content_length(
            content_length, content_encoding
        )
        truncated = known_size is not None and known_size > limit
        source_size_at_least: int | None = None
        bytes_seen = 0
        bytes_written = 0
        md5 = hashlib.md5(usedforsecurity=False)
        sha256 = hashlib.sha256()
        temp_path: Path | None = None
        stream_closed = False

        try:
            fd, raw_temp_path = tempfile.mkstemp(
                prefix=".download-", suffix=".tmp", dir=download_dir
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(fd, "wb") as target:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise DownloadError("Download stream yielded a non-bytes chunk")
                    if not chunk:
                        continue

                    observation_limit = (
                        limit if known_size is not None and known_size > limit else limit + 1
                    )
                    remaining_probe = observation_limit - bytes_seen
                    observed = chunk[:remaining_probe]
                    bytes_seen += len(observed)
                    writable = observed[: max(0, limit - bytes_written)]
                    if writable:
                        target.write(writable)
                        md5.update(writable)
                        sha256.update(writable)
                        bytes_written += len(writable)

                    if bytes_seen > limit or (known_size is not None and known_size > limit and bytes_written == limit):
                        truncated = True
                        break

                if known_size is not None and known_size > limit and bytes_written < limit:
                    raise DownloadError("Download stream ended before the declared prefix was received")

                await self._close_stream(chunks)
                stream_closed = True
                target.flush()
                os.fsync(target.fileno())

            os.chmod(temp_path, 0o600)
            os.replace(temp_path, final_path)
            temp_path = None

            if known_size is None:
                if bytes_seen > limit:
                    source_size = None
                    source_size_at_least = limit + 1
                else:
                    source_size = bytes_seen
            else:
                source_size = known_size

            if truncated:
                warning: dict[str, Any] = {
                    "op": f"{namespace}.{operation}",
                    "file_size": bytes_written,
                    "max_download_bytes": limit,
                    "truncated": True,
                    "source_size": source_size,
                    "source_size_at_least": source_size_at_least,
                }
                logger.warning("Binary download truncated", extra=warning)

            return DownloadResult(
                file_path=str(final_path),
                file_size=bytes_written,
                checksum_md5=md5.hexdigest(),
                checksum_sha256=sha256.hexdigest(),
                mime_type=mime_type,
                truncated=truncated,
                source_size=source_size,
                source_size_at_least=source_size_at_least,
            )
        except BaseException:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            self._remove_empty_directory(download_dir)
            raise
        finally:
            if not stream_closed:
                await self._close_stream(chunks)

    @staticmethod
    def _applicable_content_length(
        content_length: str | None, content_encoding: str | None
    ) -> int | None:
        encoding = (content_encoding or "identity").strip().casefold()
        if encoding not in {"", "identity"} or content_length is None:
            return None
        try:
            value = int(content_length.strip())
        except (AttributeError, ValueError):
            return None
        return value if value >= 0 else None

    @classmethod
    def _safe_filename(
        cls,
        original_filename: str | None,
        *,
        namespace: str,
        operation: str,
        mime_type: str | None,
    ) -> str:
        if original_filename is None or not original_filename.strip():
            extension = mimetypes.guess_extension(mime_type or "") or ".bin"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            original_filename = f"{namespace}_{operation}_{timestamp}{extension}"
        if "\x00" in original_filename or any(
            separator in original_filename for separator in ("/", "\\")
        ):
            raise InvalidDownloadError("Download filename contains a forbidden separator")
        candidate = Path(original_filename)
        if candidate.is_absolute() or original_filename in {".", ".."}:
            raise InvalidDownloadError("Download filename must be a relative basename")
        normalized = unicodedata.normalize("NFKC", original_filename).strip()
        sanitized = _SAFE_FILENAME.sub("_", normalized).strip("._")
        if not sanitized:
            raise InvalidDownloadError("Download filename is empty after sanitization")
        return sanitized[:255]

    @staticmethod
    def _restrict_directory(path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, 0o700)

    @staticmethod
    def _remove_empty_directory(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass

    @staticmethod
    async def _close_stream(chunks: AsyncIterable[bytes]) -> None:
        close = getattr(chunks, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result
