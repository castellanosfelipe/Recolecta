"""Concurrent atomic download engine with resume, retry, and integrity checks."""

from __future__ import annotations

import os
import random
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

from app.errors import ErrorType, RecolectaError, classify_exception, is_retryable
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


DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES = 64 * 1024 * 1024
DEFAULT_DISK_SPACE_CHECK_INTERVAL_BYTES = 8 * 1024 * 1024


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
class StagingCleanupResult:
    """Bounded aggregate returned by a recursive staging cleanup."""

    files_examined: int = 0
    files_removed: int = 0
    bytes_removed: int = 0
    errors: int = 0
    shards_removed: int = 0


@dataclass(frozen=True)
class _PreparedDownload:
    index: int
    remote_file: RemoteFile
    destination: Destination
    final_path: Path
    part_path: Path
    replace_existing: bool = False


class DownloadCancelled(RecolectaError):
    def __init__(self) -> None:
        super().__init__(
            ErrorType.INTERRUPTED,
            "La corrida fue cancelada; el archivo parcial se conserva.",
            retryable=False,
        )


class _IncrementalDiskSpaceGuard:
    """Sample free space before bounded write windows for unknown-size files."""

    def __init__(
        self,
        *,
        destination_root: Path,
        disk_usage: Callable,
        probe_lock: threading.Lock,
        sample_interval_bytes: int,
        active_slots: int,
        reserve_bytes: int,
    ) -> None:
        self.destination_root = destination_root
        self.disk_usage = disk_usage
        self.probe_lock = probe_lock
        self.sample_interval_bytes = sample_interval_bytes
        self.active_slots = active_slots
        self.reserve_bytes = reserve_bytes
        self._bytes_until_probe = 0

    def before_write(self, chunk_size: int) -> None:
        """Reject the next chunk before it can consume the safety reserve."""
        if chunk_size <= 0:
            return
        if chunk_size <= self._bytes_until_probe:
            self._bytes_until_probe -= chunk_size
            return

        own_window = max(self.sample_interval_bytes, chunk_size)
        concurrent_window = own_window + (
            self.sample_interval_bytes * max(0, self.active_slots - 1)
        )
        required = concurrent_window + self.reserve_bytes
        with self.probe_lock:
            free = self.disk_usage(self.destination_root).free
            if free < required:
                raise RecolectaError(
                    ErrorType.DISK_SPACE,
                    (
                        "Espacio insuficiente durante la descarga de tamaño "
                        f"desconocido: libres {free} bytes, requeridos "
                        f"{required} para el siguiente bloque y la reserva."
                    ),
                )
        self._bytes_until_probe = own_window - chunk_size


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
        unknown_size_reserve_bytes: int = DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES,
        disk_space_check_interval_bytes: int = (
            DEFAULT_DISK_SPACE_CHECK_INTERVAL_BYTES
        ),
        retry_waiter: (
            Callable[[threading.Event, float], bool] | None
        ) = None,
    ) -> None:
        if block_size < 1024:
            raise ValueError("El tamaño de bloque debe ser al menos 1024 bytes.")
        if unknown_size_reserve_bytes <= 0:
            raise ValueError(
                "La reserva para archivos de tamaño desconocido debe ser positiva."
            )
        if disk_space_check_interval_bytes <= 0:
            raise ValueError(
                "El intervalo de verificación de espacio debe ser positivo."
            )
        if reserve_ratio < 0:
            raise ValueError("reserve_ratio no puede ser negativo.")
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
        self.unknown_size_reserve_bytes = unknown_size_reserve_bytes
        self.disk_space_check_interval_bytes = (
            disk_space_check_interval_bytes
        )
        self.retry_waiter = retry_waiter or (
            lambda event, timeout: event.wait(timeout)
        )
        bucket_key = str(self.connection.id or self.connection.name)
        self.bandwidth_buckets = self.throttle.bandwidth_buckets(
            bucket_key, self.connection.bandwidth_limit_kbps
        )
        self._executor: ThreadPoolExecutor | None = None
        self._transport_local = threading.local()
        self._transports_lock = threading.Lock()
        self._worker_transports: set[Transport] = set()
        self._disk_space_probe_lock = threading.Lock()

    def __enter__(self) -> "DownloadEngine":
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.connection.max_parallel_files,
                thread_name_prefix="recolecta-download",
            )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        """Stop workers and close every reusable per-worker session."""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        self._close_worker_transports()

    def download_files(
        self,
        files: tuple[RemoteFile, ...],
        *,
        run_id: int,
        cancel_event: threading.Event | None = None,
        on_progress: Callable[[RemoteFile, int, int | None], None] | None = None,
        on_outcome: Callable[[DownloadOutcome], None] | None = None,
        destination_paths: Mapping[
            tuple[str, str | None, int | None], Path
        ] | None = None,
        replace_existing: bool = False,
        check_disk_space: bool = True,
    ) -> tuple[DownloadOutcome, ...]:
        """Preflight all files, then download with per-connection concurrency."""
        cancel = cancel_event or threading.Event()
        outcomes: list[DownloadOutcome | None] = [None] * len(files)
        prepared: list[_PreparedDownload] = []
        destination_root = resolve_destination_root(
            self.connection, self.portable_root
        )
        naming_time = self.wall_clock()

        for index, remote_file in enumerate(files):
            try:
                destination = build_destination(
                    self.connection,
                    remote_file,
                    portable_root=self.portable_root,
                    run_id=run_id,
                    fallback_time=naming_time,
                )
                reserved_path = (
                    destination_paths.get(remote_file.identity)
                    if destination_paths is not None
                    else None
                )
                if reserved_path is not None:
                    destination = Destination(
                        root=destination.root,
                        path=reserved_path,
                        was_truncated=(
                            destination.was_truncated
                            or reserved_path != destination.path
                        ),
                    )
                final_path = (
                    destination.path
                    if replace_existing
                    else resolve_conflict(
                        destination.path,
                        self.connection.on_conflict,
                        timestamp=self.wall_clock(),
                    )
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
                part_path = _sharded_part_path(
                    staging,
                    _part_filename(self.connection, remote_file),
                )
                prepared.append(
                    _PreparedDownload(
                        index,
                        remote_file,
                        destination,
                        final_path,
                        part_path,
                        replace_existing,
                    )
                )
            except RecolectaError as exc:
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

        # Known files reserve their remaining growth. Unknown files reserve
        # only the slots that can be active concurrently; a per-stream guard
        # samples free space if an object grows beyond that bounded allowance.
        unknown_count = sum(
            item.remote_file.size_bytes is None for item in prepared
        )
        active_unknown_slots = min(
            unknown_count,
            self.connection.max_parallel_files,
        )
        planned_bytes = sum(
            _planned_download_bytes(
                item,
                unknown_size_reserve_bytes=self.unknown_size_reserve_bytes,
            )
            for item in prepared
            if item.remote_file.size_bytes is not None
        ) + (
            active_unknown_slots * self.unknown_size_reserve_bytes
        )
        disk_arguments = {}
        if self.disk_usage is not None:
            disk_arguments["disk_usage"] = self.disk_usage
        if check_disk_space:
            ensure_disk_space(
                destination_root,
                planned_bytes,
                reserve_ratio=self.reserve_ratio,
                **disk_arguments,
            )

        prepared = [
            replace(
                item,
                part_path=_prepare_staging_path(
                    item.destination.root / ".staging",
                    item.part_path.name,
                ),
            )
            for item in prepared
        ]

        owned_pool = self._executor is None
        pool = self._executor or ThreadPoolExecutor(
            max_workers=self.connection.max_parallel_files,
            thread_name_prefix="recolecta-download",
        )
        try:
            futures = {
                pool.submit(
                    self._download_one,
                    item,
                    cancel=cancel,
                    on_progress=on_progress,
                    active_unknown_slots=active_unknown_slots,
                ): item.index
                for item in prepared
            }
            for future in as_completed(futures):
                outcome = future.result()
                outcomes[futures[future]] = outcome
                if on_outcome:
                    on_outcome(outcome)
        finally:
            if owned_pool:
                pool.shutdown(wait=True, cancel_futures=False)
                self._close_worker_transports()

        return tuple(outcome for outcome in outcomes if outcome is not None)

    def _download_one(
        self,
        item: _PreparedDownload,
        *,
        cancel: threading.Event,
        on_progress: Callable[[RemoteFile, int, int | None], None] | None,
        active_unknown_slots: int,
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
                    cancel_event=cancel,
                ) as acquired:
                    if not acquired:
                        raise DownloadCancelled()
                    result = self._attempt(
                        item,
                        cancel=cancel,
                        on_progress=on_progress,
                        active_unknown_slots=active_unknown_slots,
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
                    duration_s=self._elapsed(started),
                    path_was_truncated=item.destination.was_truncated,
                )
            except DownloadCancelled:
                self._discard_worker_transport()
                return self._cancelled_outcome(item, attempts, started)
            except Exception as exc:
                self._discard_worker_transport()
                error_type = classify_exception(exc)
                retryable = (
                    exc.retryable
                    if isinstance(exc, RecolectaError)
                    else is_retryable(error_type)
                )
                if attempts > self.connection.retries or not retryable:
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
                        duration_s=self._elapsed(started),
                        path_was_truncated=item.destination.was_truncated,
                    )
                if self.retry_waiter(
                    cancel,
                    self._backoff_delay(attempts),
                ):
                    return self._cancelled_outcome(
                        item,
                        attempts,
                        started,
                    )
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
        active_unknown_slots: int,
    ) -> _AttemptResult:
        verifier = StreamingVerifier(self.connection.verify_mode)
        disk_guard = self._unknown_size_disk_guard(
            item,
            active_unknown_slots=active_unknown_slots,
        )
        mode = "r+b" if item.part_path.exists() else "w+b"
        with item.part_path.open(mode) as target:
            target.seek(0, os.SEEK_END)
            offset = target.tell()
            transport: Transport | None = None
            resume_identity_verified = False
            if not _has_trustworthy_resume_identity(item.remote_file):
                # Size alone does not identify a remote object version.
                # Without a reliable timestamp, stale bytes must never be
                # resumed or published, even when the expected size is known.
                target.seek(0)
                target.truncate(0)
                offset = 0
            elif offset > item.remote_file.size_bytes:
                target.seek(0)
                target.truncate(0)
                offset = 0
            elif offset:
                transport = self._worker_transport()
                current = transport.stat(item.remote_file.remote_path)
                if not _same_remote_version(item.remote_file, current):
                    mismatch = _remote_version_mismatch_error(
                        item.remote_file,
                        current,
                        phase="desde el listado",
                        retry_size_change=True,
                    )
                    # A size mismatch happened before this attempt wrote any
                    # bytes. Preserve the known partial while retrying so the
                    # next attempt must revalidate it again; truncating here
                    # would fall through to an unchecked offset-zero retry.
                    if not mismatch.retryable:
                        target.seek(0)
                        target.truncate(0)
                    raise mismatch
                resume_identity_verified = True
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
                expected_size = item.remote_file.size_bytes
                next_size = verifier.bytes_seen + len(chunk)
                if expected_size is not None and next_size > expected_size:
                    raise RecolectaError(
                        ErrorType.INTEGRITY,
                        (
                            "El remoto entregó más datos de los anunciados: "
                            f"el siguiente bloque alcanzaría {next_size} bytes "
                            f"y se esperaban {expected_size}."
                        ),
                        retryable=False,
                    )
                if disk_guard is not None:
                    disk_guard.before_write(len(chunk))
                for bandwidth_bucket in self.bandwidth_buckets:
                    if not bandwidth_bucket.consume(
                        len(chunk),
                        cancel_event=cancel,
                    ):
                        raise DownloadCancelled()
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
                transport = transport or self._worker_transport()
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
            if resume_identity_verified:
                assert transport is not None
                current = transport.stat(item.remote_file.remote_path)
                if not _same_remote_version(item.remote_file, current):
                    target.seek(0)
                    target.truncate(0)
                    raise _remote_version_mismatch_error(
                        item.remote_file,
                        current,
                        phase="durante la reanudación",
                    )
            target.flush()
            os.fsync(target.fileno())
            actual_size = target.seek(0, os.SEEK_END)
            verifier.verify_size(
                actual=actual_size,
                expected=item.remote_file.size_bytes,
            )

        if item.remote_file.mtime_utc is not None:
            modified = item.remote_file.mtime_utc.timestamp()
            os.utime(item.part_path, (modified, modified))
        item.final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(item.part_path, item.final_path)
        return self._AttemptResult(
            verifier.sha256, resumed_from, resume_supported
        )

    def _unknown_size_disk_guard(
        self,
        item: _PreparedDownload,
        *,
        active_unknown_slots: int,
    ) -> _IncrementalDiskSpaceGuard | None:
        if (
            item.remote_file.size_bytes is not None
            or active_unknown_slots <= 0
        ):
            return None
        interval = max(
            self.block_size,
            min(
                self.disk_space_check_interval_bytes,
                self.unknown_size_reserve_bytes,
            ),
        )
        reserve_bytes = int(
            active_unknown_slots
            * self.unknown_size_reserve_bytes
            * self.reserve_ratio
        )
        return _IncrementalDiskSpaceGuard(
            destination_root=item.destination.root,
            disk_usage=self.disk_usage or shutil.disk_usage,
            probe_lock=self._disk_space_probe_lock,
            sample_interval_bytes=interval,
            active_slots=active_unknown_slots,
            reserve_bytes=reserve_bytes,
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
            duration_s=self._elapsed(started),
            path_was_truncated=item.destination.was_truncated,
        )

    def _backoff_delay(self, attempts: int) -> float:
        exponential = min(60.0, 2.0 ** max(0, attempts - 1))
        return exponential + self.random_value()

    def _elapsed(self, started: float) -> float:
        """Return a positive measurable interval for persisted rate metrics."""
        return max(1e-9, self.monotonic() - started)

    def _worker_transport(self) -> Transport:
        transport = getattr(self._transport_local, "transport", None)
        if transport is not None:
            return transport
        transport = self.transport_factory()
        try:
            transport.connect()
        except Exception:
            try:
                transport.close()
            finally:
                raise
        self._transport_local.transport = transport
        with self._transports_lock:
            self._worker_transports.add(transport)
        return transport

    def _discard_worker_transport(self) -> None:
        transport = getattr(self._transport_local, "transport", None)
        if transport is None:
            return
        del self._transport_local.transport
        with self._transports_lock:
            self._worker_transports.discard(transport)
        try:
            transport.close()
        except Exception:
            pass

    def _close_worker_transports(self) -> None:
        with self._transports_lock:
            transports = tuple(self._worker_transports)
            self._worker_transports.clear()
        for transport in transports:
            try:
                transport.close()
            except Exception:
                pass


def cleanup_orphaned_staging(
    staging: Path,
    *,
    active_part_names: set[str],
    cutoff: datetime | None = None,
) -> StagingCleanupResult:
    """Remove eligible partials recursively without retaining their paths."""
    files_examined = 0
    files_removed = 0
    bytes_removed = 0
    errors = 0
    shards_removed = 0
    cutoff_timestamp = None
    if cutoff is not None:
        normalized_cutoff = (
            cutoff.replace(tzinfo=timezone.utc)
            if cutoff.tzinfo is None
            else cutoff
        )
        cutoff_timestamp = normalized_cutoff.timestamp()

    try:
        root_stat = staging.lstat()
    except FileNotFoundError:
        return StagingCleanupResult()
    except OSError:
        return StagingCleanupResult(errors=1)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        return StagingCleanupResult(errors=1)

    def visit(directory: Path) -> None:
        nonlocal files_examined
        nonlocal files_removed
        nonlocal bytes_removed
        nonlocal errors
        nonlocal shards_removed

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            visit(candidate)
                            continue
                        if (
                            not entry.name.endswith(".part")
                            or not entry.is_file(follow_symlinks=False)
                        ):
                            continue
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        errors += 1
                        continue

                    files_examined += 1
                    if entry.name in active_part_names:
                        continue
                    should_remove = metadata.st_size == 0 or (
                        cutoff_timestamp is not None
                        and metadata.st_mtime < cutoff_timestamp
                    )
                    if not should_remove:
                        continue
                    try:
                        candidate.unlink()
                    except OSError:
                        errors += 1
                        continue
                    files_removed += 1
                    bytes_removed += metadata.st_size
        except OSError:
            errors += 1
            return

        if (
            directory.parent == staging
            and len(directory.name) == 2
            and all(
                character in "0123456789abcdefABCDEF"
                for character in directory.name
            )
        ):
            try:
                with os.scandir(directory) as remaining:
                    is_empty = next(remaining, None) is None
            except OSError:
                errors += 1
                return
            if is_empty:
                try:
                    directory.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    errors += 1
                else:
                    shards_removed += 1

    visit(staging)
    return StagingCleanupResult(
        files_examined=files_examined,
        files_removed=files_removed,
        bytes_removed=bytes_removed,
        errors=errors,
        shards_removed=shards_removed,
    )


def _sharded_part_path(staging: Path, part_filename: str) -> Path:
    """Return the deterministic two-hex shard for an existing UUID filename."""
    return staging / part_filename[:2] / part_filename


def _prepare_staging_path(staging: Path, part_filename: str) -> Path:
    """Migrate a legacy flat partial after preflight, with a safe fallback."""
    sharded = _sharded_part_path(staging, part_filename)
    legacy = staging / part_filename
    if sharded.exists():
        return sharded
    if legacy.exists():
        try:
            sharded.parent.mkdir(parents=True, exist_ok=True)
            os.replace(legacy, sharded)
        except OSError:
            if sharded.exists():
                return sharded
            return legacy
        return sharded
    sharded.parent.mkdir(parents=True, exist_ok=True)
    return sharded


def _planned_download_bytes(
    item: _PreparedDownload,
    *,
    unknown_size_reserve_bytes: int,
) -> int:
    """Estimate new disk growth without counting a trusted partial twice."""
    return estimate_download_bytes(
        item.destination.root,
        item.remote_file,
        part_filename=item.part_path.name,
        unknown_size_reserve_bytes=unknown_size_reserve_bytes,
    )


def estimate_download_bytes(
    destination_root: Path,
    remote_file: RemoteFile,
    *,
    connection: Connection | None = None,
    part_filename: str | None = None,
    unknown_size_reserve_bytes: int = DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES,
) -> int:
    """Estimate bytes still needed, including a floor for unknown sizes."""
    if unknown_size_reserve_bytes <= 0:
        raise ValueError(
            "La reserva para archivos de tamaño desconocido debe ser positiva."
        )
    expected = remote_file.size_bytes
    if expected is None:
        return unknown_size_reserve_bytes
    if not _has_trustworthy_resume_identity(remote_file):
        return expected
    if part_filename is None:
        if connection is None:
            raise ValueError(
                "Se requiere la conexión para estimar un staging sin nombre."
            )
        part_filename = _part_filename(connection, remote_file)
    staging = destination_root / ".staging"
    partial_size = _resumable_part_size(staging, part_filename, expected)
    return expected - partial_size


def _resumable_part_size(
    staging: Path,
    part_filename: str,
    expected_size: int,
) -> int:
    """Return a safe resumable length from the sharded or legacy layout."""
    sharded = _sharded_part_path(staging, part_filename)
    legacy = staging / part_filename
    candidate = sharded if sharded.exists() else legacy
    try:
        metadata = candidate.lstat()
    except OSError:
        return 0
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > expected_size
    ):
        return 0
    return metadata.st_size


def _has_trustworthy_resume_identity(remote_file: RemoteFile) -> bool:
    """Return whether metadata can distinguish the current remote version."""
    return (
        remote_file.size_bytes is not None
        and remote_file.mtime_utc is not None
        and remote_file.timestamp_reliable
    )


def _same_remote_version(expected: RemoteFile, current: RemoteFile) -> bool:
    """Compare version-bearing metadata before and after a resumed transfer."""
    return (
        _has_trustworthy_resume_identity(expected)
        and _has_trustworthy_resume_identity(current)
        and expected.size_bytes == current.size_bytes
        and expected.mtime_utc == current.mtime_utc
    )


def _remote_version_mismatch_error(
    expected: RemoteFile,
    current: RemoteFile,
    *,
    phase: str,
    retry_size_change: bool = False,
) -> RecolectaError:
    """Classify a remote identity change without allowing unchecked resume."""
    if retry_size_change and expected.size_bytes != current.size_bytes:
        return RecolectaError(
            ErrorType.PARTIAL_TRANSFER,
            (
                f"El tamaño remoto cambió {phase}: esperado "
                f"{expected.size_bytes}, actual {current.size_bytes}. El "
                "parcial se conservó pendiente de una nueva validación."
            ),
            retryable=True,
        )
    return RecolectaError(
        ErrorType.INTEGRITY,
        (
            f"La versión remota cambió {phase}; el parcial se descartó "
            "para evitar mezclar versiones y no se publicó."
        ),
        retryable=False,
    )


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
