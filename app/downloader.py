"""Concurrent atomic download engine with resume, retry, and integrity checks."""

from __future__ import annotations

import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

from app.errors import ErrorType, HarvesterError, classify_exception, is_retryable
from app.integrity import StreamingVerifier, ensure_disk_space
from app.models import Connection
from app.naming import (
    Destination,
    build_destination,
    resolve_conflict,
    resolve_destination_root,
)
from app.throttle import ThrottleManager
from app.transports.base import RemoteFile, Transport


class DownloadStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DownloadOutcome:
    remote_file: RemoteFile
    status: DownloadStatus
    local_path: Path | None
    attempts: int
    bytes_done: int
    sha256: str | None = None
    error_type: ErrorType | None = None
    error_msg: str = ""
    resumed_from: int = 0
    resume_supported: bool = True
    duration_s: float = 0.0
    path_was_truncated: bool = False


@dataclass(frozen=True)
class _PreparedDownload:
    index: int
    remote_file: RemoteFile
    destination: Destination
    final_path: Path
    part_path: Path


class DownloadCancelled(HarvesterError):
    def __init__(self) -> None:
        super().__init__(
            ErrorType.INTERRUPTED,
            "La corrida fue cancelada; el archivo parcial se conserva.",
            retryable=False,
        )


class DownloadEngine:
    """Download a planned set while preserving atomicity and bounded impact."""

    def __init__(
        self,
        connection: Connection,
        *,
        portable_root: Path,
        transport_factory: Callable[[], Transport],
        throttle: ThrottleManager | None = None,
        block_size: int = 64 * 1024,
        minimum_spacing_s: float = 0.0,
        reserve_ratio: float = 0.10,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        random_value: Callable[[], float] = random.random,
        disk_usage=None,
    ) -> None:
        if block_size < 1024:
            raise ValueError("El tamaño de bloque debe ser al menos 1024 bytes.")
        self.connection = connection.normalized()
        self.portable_root = portable_root.resolve(strict=False)
        self.transport_factory = transport_factory
        self.throttle = throttle or ThrottleManager(
            global_parallelism=max(1, self.connection.max_parallel_files),
            clock=monotonic,
            sleeper=sleeper,
        )
        self.block_size = block_size
        self.minimum_spacing_s = minimum_spacing_s
        self.reserve_ratio = reserve_ratio
        self.sleeper = sleeper
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.random_value = random_value
        self.disk_usage = disk_usage
        bucket_key = str(self.connection.id or self.connection.name)
        self.bandwidth_bucket = self.throttle.bandwidth_bucket(
            bucket_key, self.connection.bandwidth_limit_kbps
        )

    def download_files(
        self,
        files: tuple[RemoteFile, ...],
        *,
        run_id: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[RemoteFile, int, int | None], None] | None = None,
        on_outcome: Callable[[DownloadOutcome], None] | None = None,
    ) -> tuple[DownloadOutcome, ...]:
        """Preflight all files, then download with per-connection concurrency."""
        cancel = cancel_event or threading.Event()
        outcomes: list[DownloadOutcome | None] = [None] * len(files)
        prepared: list[_PreparedDownload] = []
        destination_root = resolve_destination_root(
            self.connection, self.portable_root
        )

        for index, remote_file in enumerate(files):
            try:
                destination = build_destination(
                    self.connection,
                    remote_file,
                    portable_root=self.portable_root,
                    run_id=run_id,
                )
                final_path = resolve_conflict(
                    destination.path,
                    self.connection.on_conflict,
                    timestamp=self.wall_clock(),
                )
                if final_path is None:
                    outcomes[index] = DownloadOutcome(
                        remote_file,
                        DownloadStatus.SKIPPED,
                        destination.path,
                        attempts=0,
                        bytes_done=0,
                        path_was_truncated=destination.was_truncated,
                    )
                    if on_outcome:
                        on_outcome(outcomes[index])
                    continue
                staging = destination.root / ".staging"
                part_path = staging / _part_filename(self.connection, remote_file)
                prepared.append(
                    _PreparedDownload(
                        index,
                        remote_file,
                        destination,
                        final_path,
                        part_path,
                    )
                )
            except HarvesterError as exc:
                outcomes[index] = DownloadOutcome(
                    remote_file,
                    DownloadStatus.FAILED,
                    None,
                    attempts=0,
                    bytes_done=0,
                    error_type=exc.error_type,
                    error_msg=str(exc),
                )
                if on_outcome:
                    on_outcome(outcomes[index])

        planned_bytes = sum(
            item.remote_file.size_bytes or 0 for item in prepared
        )
        disk_arguments = {}
        if self.disk_usage is not None:
            disk_arguments["disk_usage"] = self.disk_usage
        ensure_disk_space(
            destination_root,
            planned_bytes,
            reserve_ratio=self.reserve_ratio,
            **disk_arguments,
        )

        for item in prepared:
            item.part_path.parent.mkdir(parents=True, exist_ok=True)

        with ThreadPoolExecutor(
            max_workers=self.connection.max_parallel_files,
            thread_name_prefix="harvester-download",
        ) as pool:
            futures = {
                pool.submit(
                    self._download_one,
                    item,
                    cancel=cancel,
                    on_progress=on_progress,
                ): item.index
                for item in prepared
            }
            for future in as_completed(futures):
                outcome = future.result()
                outcomes[futures[future]] = outcome
                if on_outcome:
                    on_outcome(outcome)

        return tuple(outcome for outcome in outcomes if outcome is not None)

    def _download_one(
        self,
        item: _PreparedDownload,
        *,
        cancel: threading.Event,
        on_progress: Callable[[RemoteFile, int, int | None], None] | None,
    ) -> DownloadOutcome:
        started = self.monotonic()
        attempts = 0
        resume_supported = True
        last_resumed_from = 0
        while attempts <= self.connection.retries:
            if cancel.is_set():
                return self._cancelled_outcome(item, attempts, started)
            attempts += 1
            try:
                with self.throttle.transfer_slot(
                    self.connection.host,
                    minimum_spacing_s=self.minimum_spacing_s,
                ):
                    result = self._attempt(
                        item,
                        cancel=cancel,
                        on_progress=on_progress,
                    )
                resume_supported = resume_supported and result.resume_supported
                last_resumed_from = result.resumed_from
                return DownloadOutcome(
                    item.remote_file,
                    DownloadStatus.OK,
                    item.final_path,
                    attempts,
                    item.final_path.stat().st_size,
                    result.sha256,
                    resumed_from=last_resumed_from,
                    resume_supported=resume_supported,
                    duration_s=max(0.0, self.monotonic() - started),
                    path_was_truncated=item.destination.was_truncated,
                )
            except DownloadCancelled:
                return self._cancelled_outcome(item, attempts, started)
            except Exception as exc:
                error_type = classify_exception(exc)
                if attempts > self.connection.retries or not is_retryable(error_type):
                    return DownloadOutcome(
                        item.remote_file,
                        DownloadStatus.FAILED,
                        item.final_path,
                        attempts,
                        _safe_size(item.part_path),
                        error_type=error_type,
                        error_msg=str(exc),
                        resumed_from=last_resumed_from,
                        resume_supported=resume_supported,
                        duration_s=max(0.0, self.monotonic() - started),
                        path_was_truncated=item.destination.was_truncated,
                    )
                self.sleeper(self._backoff_delay(attempts))
        raise AssertionError("Bucle de reintentos inalcanzable.")

    @dataclass(frozen=True)
    class _AttemptResult:
        sha256: str | None
        resumed_from: int
        resume_supported: bool

    def _attempt(
        self,
        item: _PreparedDownload,
        *,
        cancel: threading.Event,
        on_progress: Callable[[RemoteFile, int, int | None], None] | None,
    ) -> _AttemptResult:
        verifier = StreamingVerifier(self.connection.verify_mode)
        mode = "r+b" if item.part_path.exists() else "w+b"
        with item.part_path.open(mode) as target:
            target.seek(0, os.SEEK_END)
            offset = target.tell()
            if (
                item.remote_file.size_bytes is not None
                and offset > item.remote_file.size_bytes
            ):
                target.seek(0)
                target.truncate(0)
                offset = 0
            verifier.seed_from_partial(
                target, length=offset, block_size=self.block_size
            )

            def on_restart() -> None:
                verifier.reset()
                if on_progress:
                    on_progress(item.remote_file, 0, item.remote_file.size_bytes)

            def on_chunk(chunk: bytes) -> None:
                if cancel.is_set():
                    raise DownloadCancelled()
                if self.bandwidth_bucket is not None:
                    self.bandwidth_bucket.consume(len(chunk))
                verifier.update(chunk)
                if on_progress:
                    on_progress(
                        item.remote_file,
                        verifier.bytes_seen,
                        item.remote_file.size_bytes,
                    )

            if (
                item.remote_file.size_bytes is not None
                and offset == item.remote_file.size_bytes
            ):
                resumed_from = offset
                resume_supported = True
            else:
                with self.transport_factory() as transport:
                    transfer = transport.download_to(
                        item.remote_file.remote_path,
                        target,
                        offset=offset,
                        block_size=self.block_size,
                        on_chunk=on_chunk,
                        on_restart=on_restart,
                    )
                resumed_from = transfer.resumed_from
                resume_supported = transfer.resume_supported
            target.flush()
            os.fsync(target.fileno())
            actual_size = target.seek(0, os.SEEK_END)
            verifier.verify_size(
                actual=actual_size,
                expected=item.remote_file.size_bytes,
            )

        item.final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(item.part_path, item.final_path)
        if item.remote_file.mtime_utc is not None:
            modified = item.remote_file.mtime_utc.timestamp()
            os.utime(item.final_path, (modified, modified))
        return self._AttemptResult(
            verifier.sha256, resumed_from, resume_supported
        )

    def _cancelled_outcome(
        self,
        item: _PreparedDownload,
        attempts: int,
        started: float,
    ) -> DownloadOutcome:
        return DownloadOutcome(
            item.remote_file,
            DownloadStatus.CANCELLED,
            item.final_path,
            attempts,
            _safe_size(item.part_path),
            error_type=ErrorType.INTERRUPTED,
            error_msg="La corrida fue cancelada; el parcial se conserva.",
            duration_s=max(0.0, self.monotonic() - started),
            path_was_truncated=item.destination.was_truncated,
        )

    def _backoff_delay(self, attempts: int) -> float:
        exponential = min(60.0, 2.0 ** max(0, attempts - 1))
        return exponential + self.random_value()


def cleanup_orphaned_staging(
    staging: Path, *, active_part_names: set[str]
) -> tuple[Path, ...]:
    """Delete only `.part` files not referenced by recovered pending work."""
    if not staging.exists():
        return ()
    removed: list[Path] = []
    for candidate in staging.glob("*.part"):
        if candidate.name in active_part_names:
            continue
        candidate.unlink()
        removed.append(candidate)
    return tuple(removed)


def _part_filename(connection: Connection, remote_file: RemoteFile) -> str:
    identity = "|".join(
        (
            str(connection.id or connection.name),
            remote_file.remote_path,
            remote_file.mtime_utc.isoformat() if remote_file.mtime_utc else "",
            str(remote_file.size_bytes),
        )
    )
    return f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
