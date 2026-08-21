"""Common transport contracts and remote-file metadata."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO, Callable
from types import TracebackType


@dataclass(frozen=True)
class RemoteFile:
    """Protocol-neutral metadata used by planning and download phases."""

    remote_path: str
    size_bytes: int | None
    mtime_utc: datetime | None
    timestamp_reliable: bool = True
    timestamp_source: str = ""
    is_symlink: bool = False

    def __post_init__(self) -> None:
        if self.mtime_utc is not None:
            if self.mtime_utc.tzinfo is None:
                raise ValueError("mtime_utc debe incluir zona horaria.")
            object.__setattr__(
                self,
                "mtime_utc",
                self.mtime_utc.astimezone(timezone.utc),
            )
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("El tamaño remoto no puede ser negativo.")

    @property
    def name(self) -> str:
        return self.remote_path.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def identity(self) -> tuple[str, str | None, int | None]:
        timestamp = (
            self.mtime_utc.isoformat(timespec="seconds")
            if self.mtime_utc is not None
            else None
        )
        return (self.remote_path, timestamp, self.size_bytes)


@dataclass(frozen=True)
class ListingResult:
    """Files plus non-fatal precision or compatibility observations.

    ``warnings`` is retained as the transport-level compatibility field; the
    orchestrator promotes these values to run ``notices``, not incidents.
    """

    files: tuple[RemoteFile, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TransferResult:
    """Protocol result needed to audit resume behavior."""

    bytes_received: int
    resumed_from: int
    resume_supported: bool = True


class DirectoryWorkQueue:
    """Disk-backed traversal queue that does not retain huge trees in RAM."""

    def __init__(self, roots: Iterable[tuple[str, int]]) -> None:
        self._database = sqlite3.connect("")
        self._database.execute("PRAGMA temp_store = FILE")
        self._database.execute(
            """
            CREATE TABLE directory_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                depth INTEGER NOT NULL
            )
            """
        )
        self._cursor_id = 0
        self.add_many(roots)

    def __enter__(self) -> "DirectoryWorkQueue":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def add(self, path: str, depth: int) -> None:
        self._database.execute(
            "INSERT OR IGNORE INTO directory_queue(path, depth) VALUES (?, ?)",
            (path, max(0, depth)),
        )

    def add_many(self, items: Iterable[tuple[str, int]]) -> None:
        self._database.executemany(
            """
            INSERT OR IGNORE INTO directory_queue(path, depth)
            VALUES (?, ?)
            """,
            ((path, max(0, depth)) for path, depth in items),
        )

    def pop(self) -> tuple[str, int] | None:
        row = self._database.execute(
            """
            SELECT id, path, depth
            FROM directory_queue
            WHERE id > ?
            ORDER BY id
            LIMIT 1
            """,
            (self._cursor_id,),
        ).fetchone()
        if row is None:
            return None
        self._cursor_id = int(row[0])
        return str(row[1]), int(row[2])

    def close(self) -> None:
        self._database.close()


class Transport(ABC):
    """Synchronous listing/stat interface shared by every protocol."""

    def set_listing_progress_callback(
        self,
        callback: Callable[[str, int, bool, int], None] | None,
    ) -> None:
        """Observe each remote location as recursive discovery reaches it.

        The callback is intentionally lightweight and protocol-neutral.  It
        lets the coordinator publish a heartbeat even when a wide directory
        tree has not yielded its first file yet.
        """
        self._listing_progress_callback = callback

    def _report_listing_location(
        self,
        path: str,
        depth: int,
        *,
        count_location: bool = True,
        entries_delta: int = 0,
    ) -> None:
        callback = getattr(self, "_listing_progress_callback", None)
        if callback is not None:
            callback(
                path,
                max(0, depth),
                count_location,
                max(0, entries_delta),
            )

    @property
    def last_listing_warnings(self) -> tuple[str, ...]:
        """Return non-fatal observations from the most recent listing."""
        return getattr(self, "_last_listing_warnings", ())

    def _reset_listing_warnings(self) -> None:
        self._last_listing_warnings: tuple[str, ...] = ()

    def _add_listing_warning(self, warning: str) -> None:
        if warning not in self.last_listing_warnings:
            self._last_listing_warnings = (
                *self.last_listing_warnings,
                warning,
            )

    def __enter__(self) -> "Transport":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @abstractmethod
    def connect(self) -> None:
        """Open and authenticate the protocol session."""

    @abstractmethod
    def close(self) -> None:
        """Close the protocol session without raising during cleanup."""

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> Iterator[RemoteFile]:
        """Iterate files without requiring a protocol-wide inventory.

        The fallback keeps third-party and test transports that still
        implement only ``list_files`` compatible. Production adapters
        override this method with a genuinely incremental implementation.
        """
        self._reset_listing_warnings()
        legacy_listing = type(self).list_files
        if legacy_listing is Transport.list_files:
            raise NotImplementedError(
                "El transporte debe implementar iter_files o list_files."
            )
        result = legacy_listing(
            self,
            remote_paths,
            recursive=recursive,
            max_depth=max_depth,
        )
        self._last_listing_warnings = result.warnings
        return iter(result.files)

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        """Materialize an incremental listing for legacy callers."""
        files = tuple(
            self.iter_files(
                remote_paths,
                recursive=recursive,
                max_depth=max_depth,
            )
        )
        return ListingResult(files, self.last_listing_warnings)

    @abstractmethod
    def stat(self, remote_path: str) -> RemoteFile:
        """Return current metadata for one remote file."""

    @abstractmethod
    def download_to(
        self,
        remote_path: str,
        target: BinaryIO,
        *,
        offset: int,
        block_size: int,
        on_chunk: Callable[[bytes], None],
        on_restart: Callable[[], None],
    ) -> TransferResult:
        """Stream a remote file into an already opened staging file."""
