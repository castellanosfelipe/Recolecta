"""Common transport contracts and remote-file metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    """Files plus non-fatal precision or protocol warnings."""

    files: tuple[RemoteFile, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


class Transport(ABC):
    """Synchronous listing/stat interface shared by every protocol."""

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

    @abstractmethod
    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        """List files below one or more configured roots."""

    @abstractmethod
    def stat(self, remote_path: str) -> RemoteFile:
        """Return current metadata for one remote file."""
