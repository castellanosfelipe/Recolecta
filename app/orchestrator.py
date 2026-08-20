"""Time-window calculation and side-effect-free run planning."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
import sqlite3
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import AppPaths
from app.alerts import AlertManager
from app.connection_validation import (
    ConnectionValidationResult,
    validate_connection_paths,
)
from app.db import ConnectionRepository, Database, RunRepository
from app.downloader import (
    DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES,
    DownloadEngine,
    DownloadOutcome,
    DownloadStatus,
    estimate_download_bytes,
)
from app.errors import ErrorType, RecolectaError, classify_exception
from app.integrity import ensure_disk_space
from app.models import Connection, Protocol, WindowMode
from app.naming import (
    build_destination,
    collision_path,
    local_path_key,
    resolve_destination_root,
)
from app.progress import ProgressRegistry
from app.run_logging import RunEventLog, RunLogStore
from app.statuses import enrich_run
from app.throttle import ThrottleManager
from app.transports import create_transport
from app.transports.base import ListingResult, RemoteFile, Transport

logger = logging.getLogger(__name__)

DISCOVERY_BATCH_SIZE = 500
PLAN_SAMPLE_LIMIT = 500
OUTCOME_SAMPLE_LIMIT = 500
QUEUE_BATCH_MAX = 64
FULL_SCAN_MAX_DEPTH = 2_147_483_647
LOCAL_MTIME_TOLERANCE_S = 2.0
RUN_LOG_DETAIL_LIMIT = 1_000
SYSTEMIC_DOWNLOAD_ERRORS = frozenset(
    {
        ErrorType.AUTH,
        ErrorType.DNS,
        ErrorType.PERMISSION,
        ErrorType.PROTOCOL,
        ErrorType.TCP_CONNECT,
        ErrorType.TCP_TIMEOUT,
        ErrorType.TLS,
        ErrorType.PARTIAL_TRANSFER,
        ErrorType.UNKNOWN,
    }
)


@dataclass(frozen=True)
class TimeWindow:
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self) -> None:
        if self.start_utc.tzinfo is None or self.end_utc.tzinfo is None:
            raise ValueError("La ventana debe incluir zona horaria.")
        if self.start_utc >= self.end_utc:
            raise ValueError("El inicio de la ventana debe ser anterior al fin.")


class PlanStatus(StrEnum):
    PLANNED = "planned"
    DUPLICATE = "duplicate"
    OUTSIDE_WINDOW = "outside_window"
    QUIET_PERIOD = "quiet_period"
    INCLUDE_FILTER = "include_filter"
    EXCLUDE_FILTER = "exclude_filter"
    SIZE_FILTER = "size_filter"
    SYMLINK = "symlink"
    TIMESTAMP_MISSING = "timestamp_missing"
    LOCAL_PRESENT = "local_present"
    LOCAL_MISSING = "local_missing"
    LOCAL_DIFFERENT = "local_different"
    PATH_INVALID = "path_invalid"


@dataclass(frozen=True)
class PlanItem:
    file: RemoteFile
    status: PlanStatus
    reason: str = ""


@dataclass(frozen=True)
class DryRunPlan:
    connection_id: int | None
    window: TimeWindow
    items: tuple[PlanItem, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    notices: tuple[str, ...] = field(default_factory=tuple)
    total_items: int | None = None
    counter_totals: dict[str, int] | None = None
    planned_total: int | None = None
    planned_bytes: int = 0
    scan_mode: str = "window"

    @property
    def files_to_download(self) -> tuple[RemoteFile, ...]:
        return tuple(
            item.file
            for item in self.items
            if _requires_download(item.status)
        )

    @property
    def files_found_count(self) -> int:
        return self.total_items if self.total_items is not None else len(self.items)

    @property
    def files_to_download_count(self) -> int:
        if self.planned_total is not None:
            return self.planned_total
        return len(self.files_to_download)

    @property
    def items_truncated(self) -> bool:
        return self.files_found_count > len(self.items)

    @property
    def is_partial(self) -> bool:
        return bool(
            self.warnings
            or self.counters.get(PlanStatus.PATH_INVALID.value, 0)
        )

    @property
    def counters(self) -> dict[str, int]:
        if self.counter_totals is not None:
            return dict(self.counter_totals)
        values = {status.value: 0 for status in PlanStatus}
        for item in self.items:
            values[item.status.value] += 1
        return values


@dataclass(frozen=True)
class RunExecution:
    connection_id: int
    run_id: int | None
    trigger: str
    status: str
    plan: DryRunPlan
    outcomes: tuple[DownloadOutcome, ...] = field(default_factory=tuple)
    outcome_counts: dict[str, int] | None = None
    outcomes_truncated: bool = False
    discovery_scope: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        outcomes = self.outcome_counts or {
            status.value: sum(
                outcome.status == status for outcome in self.outcomes
            )
            for status in DownloadStatus
        }
        summary = {
            "connection_id": self.connection_id,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": self.status,
            "window_start_utc": self.plan.window.start_utc.isoformat(),
            "window_end_utc": self.plan.window.end_utc.isoformat(),
            "files_found": self.plan.files_found_count,
            "files_planned": self.plan.files_to_download_count,
            "scan_mode": self.plan.scan_mode,
            "discovery_scope": (
                dict(self.discovery_scope)
                if self.discovery_scope is not None
                else None
            ),
            "items_truncated": self.plan.items_truncated,
            "outcomes_truncated": self.outcomes_truncated,
            "warnings": list(self.plan.warnings),
            "notices": list(self.plan.notices),
            "outcomes": outcomes,
        }
        result = enrich_run(
            {
                "status": self.status,
                "files_found": self.plan.files_found_count,
                "files_downloaded": outcomes[DownloadStatus.OK.value],
                "files_failed": outcomes[DownloadStatus.FAILED.value],
                "scan_mode": self.plan.scan_mode,
                "discovery_scope": self.discovery_scope,
            }
        )
        summary["result_status"] = result["result_status"]
        summary["status_label"] = result["status_label"]
        summary["status_detail"] = result["status_detail"]
        summary["discovery_scope"] = result["discovery_scope"]
        return summary


def _effective_discovery_scope(connection: Connection) -> dict[str, object]:
    full_scan = connection.full_local_reconciliation
    return {
        "remote_paths": list(connection.remote_paths),
        "recursive": True if full_scan else connection.recursive,
        "max_depth": (
            FULL_SCAN_MAX_DEPTH if full_scan else connection.max_depth
        ),
    }


class RunCoordinator:
    """Execute dry-runs or persisted downloads for scheduler, CLI, and API."""

    def __init__(
        self,
        database: Database,
        connections: ConnectionRepository,
        paths: AppPaths,
        *,
        throttle: ThrottleManager | None = None,
        progress: ProgressRegistry | None = None,
        run_logs: RunLogStore | None = None,
        alerts: AlertManager | None = None,
        minimum_spacing_s: float = 0.0,
        reserve_ratio: float = 0.10,
        global_bandwidth_limit_kbps: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.connections = connections
        self.runs = RunRepository(database)
        self.paths = paths
        self.throttle = throttle or ThrottleManager(
            global_parallelism=4,
            global_bandwidth_limit_kbps=global_bandwidth_limit_kbps,
        )
        self.throttle.set_global_bandwidth_limit(
            global_bandwidth_limit_kbps
        )
        self.progress = progress or ProgressRegistry(
            persist_progress=self.runs.update_file_progress
        )
        self.run_logs = run_logs or RunLogStore(paths.run_logs)
        self.alerts = alerts
        self.minimum_spacing_s = minimum_spacing_s
        self.reserve_ratio = reserve_ratio
        self.global_bandwidth_limit_kbps = global_bandwidth_limit_kbps
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._state_lock = threading.Lock()
        self._connection_locks: dict[int, threading.Lock] = {}
        self._cancel_events: dict[int, threading.Event] = {}

    def validate_connection_draft(
        self,
        connection: Connection,
        *,
        secret: str | None,
    ) -> ConnectionValidationResult:
        """Validate an unsaved draft without downloading or persisting it."""
        if connection.protocol == Protocol.SFTP:
            with tempfile.TemporaryDirectory(
                prefix="recolecta-known-hosts-"
            ) as temporary:
                temporary_known_hosts = Path(temporary) / "known_hosts"
                if self.paths.known_hosts.is_file():
                    try:
                        shutil.copyfile(
                            self.paths.known_hosts,
                            temporary_known_hosts,
                        )
                    except OSError as exc:
                        raise RecolectaError(
                            ErrorType.DISK_WRITE,
                            (
                                "No se pudo preparar una copia temporal de "
                                "known_hosts para validar SFTP."
                            ),
                        ) from exc
                return validate_connection_paths(
                    connection,
                    secret=secret,
                    portable_root=self.paths.root,
                    known_hosts=temporary_known_hosts,
                )
        return validate_connection_paths(
            connection,
            secret=secret,
            portable_root=self.paths.root,
            known_hosts=self.paths.known_hosts,
        )

    def execute_connection(
        self,
        connection_id: int,
        *,
        trigger: str,
        selected_date: date | None = None,
        dry_run_only: bool = False,
        started_at: datetime | None = None,
        window_reference_at: datetime | None = None,
    ) -> RunExecution:
        connection, lock = self._reserve_connection(
            connection_id,
            dry_run_only=dry_run_only,
        )
        return self._execute_reserved_connection(
            connection,
            lock=lock,
            trigger=trigger,
            selected_date=selected_date,
            dry_run_only=dry_run_only,
            started_at=started_at,
            window_reference_at=window_reference_at,
        )

    def submit_connection(
        self,
        connection_id: int,
        *,
        trigger: str,
        selected_date: date | None = None,
    ) -> threading.Thread:
        """Reserve synchronously and execute in a background worker."""
        connection, lock = self._reserve_connection(
            connection_id,
            dry_run_only=False,
        )

        def execute() -> None:
            try:
                self._execute_reserved_connection(
                    connection,
                    lock=lock,
                    trigger=trigger,
                    selected_date=selected_date,
                )
            except Exception:
                logger.exception(
                    "La corrida en segundo plano de la conexión %s falló.",
                    connection_id,
                )

        worker = threading.Thread(
            target=execute,
            name=f"manual-run-{connection_id}",
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            lock.release()
            raise
        return worker

    def delete_connection(self, connection_id: int) -> bool:
        """Delete only while no in-process or persisted run is active."""
        lock = self._connection_lock(connection_id)
        if not lock.acquire(blocking=False):
            raise RecolectaError(
                ErrorType.INTERRUPTED,
                f"Ya hay una corrida activa para la conexión {connection_id}.",
            )
        try:
            self.connections.get(connection_id)
            return self.connections.delete_if_idle(connection_id)
        finally:
            lock.release()

    def _reserve_connection(
        self,
        connection_id: int,
        *,
        dry_run_only: bool,
    ) -> tuple[Connection, threading.Lock]:
        lock = self._connection_lock(connection_id)
        if not lock.acquire(blocking=False):
            raise RecolectaError(
                ErrorType.INTERRUPTED,
                f"Ya hay una corrida activa para la conexión {connection_id}.",
            )
        try:
            connection = self.connections.get(connection_id)
            if not connection.enabled and not dry_run_only:
                raise RecolectaError(
                    ErrorType.INTERRUPTED,
                    f"La conexión {connection.name} está en pausa.",
                )
            if self.runs.has_active_run(connection_id):
                raise RecolectaError(
                    ErrorType.INTERRUPTED,
                    f"Ya hay una corrida activa para {connection.name}.",
                )
            return connection, lock
        except BaseException:
            lock.release()
            raise

    def _execute_reserved_connection(
        self,
        connection: Connection,
        *,
        lock: threading.Lock,
        trigger: str,
        selected_date: date | None = None,
        dry_run_only: bool = False,
        started_at: datetime | None = None,
        window_reference_at: datetime | None = None,
    ) -> RunExecution:
        connection_id = connection.id
        if connection_id is None:
            lock.release()
            raise ValueError("La conexión debe estar guardada antes de ejecutarse.")
        try:
            actual_started = started_at or self.now()
            reference = window_reference_at or actual_started
            if actual_started.tzinfo is None or reference.tzinfo is None:
                raise ValueError("started_at debe incluir zona horaria.")
            last_end = self.runs.last_successful_end(connection_id)
            expected_window = calculate_window(
                connection,
                started_at=reference,
                last_successful_end_utc=last_end,
                selected_date=selected_date,
            )
            if (
                not dry_run_only
                and not connection.full_local_reconciliation
                and trigger in {"schedule", "catchup"}
                and self.runs.has_successful_window(
                    connection_id,
                    window_start_utc=expected_window.start_utc,
                    window_end_utc=expected_window.end_utc,
                )
            ):
                return RunExecution(
                    connection_id,
                    None,
                    trigger,
                    "already_completed",
                    DryRunPlan(
                        connection_id=connection_id,
                        window=expected_window,
                        items=(),
                        total_items=0,
                        counter_totals={
                            status.value: 0 for status in PlanStatus
                        },
                        planned_total=0,
                    ),
                    discovery_scope=_effective_discovery_scope(connection),
                )
            try:
                secret = self.connections.get_secret(connection_id)
                listing_transport = create_transport(
                    connection,
                    secret=secret,
                    known_hosts=self.paths.known_hosts,
                )
            except Exception as exc:
                if not dry_run_only:
                    self._record_preflight_failure(
                        connection,
                        trigger=trigger,
                        started_at=actual_started,
                        window=expected_window,
                        exc=exc,
                    )
                raise
            if dry_run_only:
                plan = dry_run(
                    connection,
                    listing_transport,
                    started_at=reference,
                    database=self.database,
                    last_successful_end_utc=last_end,
                    selected_date=selected_date,
                    portable_root=self.paths.root,
                )
                return RunExecution(
                    connection_id,
                    None,
                    trigger,
                    "dry_run",
                    plan,
                    discovery_scope=_effective_discovery_scope(connection),
                )
            return self._execute_queued(
                connection,
                secret=secret,
                trigger=trigger,
                started_at=actual_started,
                window=expected_window,
                listing_transport=listing_transport,
            )
        finally:
            lock.release()

    def execute_all(
        self,
        *,
        trigger: str,
        selected_date: date | None = None,
        dry_run_only: bool = False,
    ) -> tuple[RunExecution, ...]:
        results = []
        for connection in self.connections.list(enabled_only=True):
            if connection.id is None:
                continue
            results.append(
                self.execute_connection(
                    connection.id,
                    trigger=trigger,
                    selected_date=selected_date,
                    dry_run_only=dry_run_only,
                )
            )
        return tuple(results)

    def cancel(self, run_id: int) -> bool:
        with self._state_lock:
            event = self._cancel_events.get(run_id)
        if event is None:
            return False
        event.set()
        self.progress.mark_cancel_requested(run_id)
        return True

    def _execute_queued(
        self,
        connection: Connection,
        *,
        secret: str | None,
        trigger: str,
        started_at: datetime,
        window: TimeWindow,
        listing_transport: Transport,
    ) -> RunExecution:
        assert connection.id is not None
        scan_mode = (
            "full_local_reconciliation"
            if connection.full_local_reconciliation
            else "window"
        )
        discovery_scope = _effective_discovery_scope(connection)
        discovery_recursive = bool(discovery_scope["recursive"])
        discovery_max_depth = int(discovery_scope["max_depth"])
        run_id = self.runs.start_run(
            connection_id=connection.id,
            trigger=trigger,
            window_start_utc=window.start_utc,
            window_end_utc=window.end_utc,
            started_at=started_at,
            scan_mode=scan_mode,
            remote_paths=connection.remote_paths,
            recursive=discovery_recursive,
            max_depth=discovery_max_depth,
        )
        run_log: RunEventLog | None = None
        seen_paths: sqlite3.Connection | None = None
        cancel = threading.Event()
        try:
            run_log = self.run_logs.create(
                run_id=run_id,
                connection_name=connection.name,
                started_at=started_at,
            )
            run_log.write(
                "run_started",
                run_id=run_id,
                connection_id=connection.id,
                connection_name=connection.name,
                trigger=trigger,
                window_start_utc=window.start_utc.isoformat(),
                window_end_utc=window.end_utc.isoformat(),
                scan_mode=scan_mode,
            )
            self.progress.start_run(
                run_id=run_id,
                connection_id=connection.id,
                connection_name=connection.name,
                trigger=trigger,
                files=(),
                bounded=True,
                phase="discovering",
                total_files=0,
                total_size_bytes=0,
            )
            with self._state_lock:
                self._cancel_events[run_id] = cancel
            counters = {status.value: 0 for status in PlanStatus}
            plan_sample: list[PlanItem] = []
            warnings: list[str] = []
            notices: list[str] = []
            total_items = 0
            planned_total = 0
            planned_bytes = 0
            estimated_disk_growth = 0
            largest_staging_extra = 0
            known_sized_planned = 0
            unknown_sized_planned = 0
            unreliable_timestamps = 0
            missing_timestamps = 0
            mapping_scope = _mapping_scope(connection, self.paths.root)
            destination_root = resolve_destination_root(
                connection,
                self.paths.root,
            )
            seen_paths = sqlite3.connect("")
            seen_paths.execute("PRAGMA temp_store = FILE")
            seen_paths.execute(
                "CREATE TABLE seen_paths (path_key TEXT PRIMARY KEY)"
            )
        except Exception as exc:
            error_type = classify_exception(exc)
            self.runs.finish_run(
                run_id,
                status="failed",
                error_type=error_type.value,
                error_msg=str(exc),
            )
            if run_log is not None:
                try:
                    run_log.write(
                        "run_finished",
                        run_id=run_id,
                        status="failed",
                        error_type=error_type.value,
                        error_msg=str(exc),
                    )
                except Exception:
                    logger.exception(
                        "No se pudo registrar el fallo inicial de la corrida %s.",
                        run_id,
                    )
            self.progress.finish_run(run_id)
            with self._state_lock:
                self._cancel_events.pop(run_id, None)
            if seen_paths is not None:
                seen_paths.close()
            self._evaluate_alerts(run_id)
            raise
        assert run_log is not None
        assert seen_paths is not None
        engine: DownloadEngine | None = None
        try:
            includes, excludes = _split_globs(
                connection.include_globs,
                connection.exclude_globs,
            )
            quiet_before = started_at.astimezone(timezone.utc) - timedelta(
                seconds=connection.quiet_period_s
            )
            with _listing_iterator(
                listing_transport,
                connection.remote_paths,
                recursive=discovery_recursive,
                max_depth=discovery_max_depth,
                notices=notices,
            ) as discovered:
                for remote_batch in _iter_batches(
                    discovered,
                    DISCOVERY_BATCH_SIZE,
                    stop_event=cancel,
                ):
                    if cancel.is_set():
                        break
                    unique_batch = tuple(
                        remote_file
                        for remote_file in remote_batch
                        if seen_paths.execute(
                            """
                            INSERT OR IGNORE INTO seen_paths(path_key)
                            VALUES (?)
                            """,
                            (_plan_path_key(connection, remote_file),),
                        ).rowcount
                    )
                    if not unique_batch:
                        continue
                    for remote_file in unique_batch:
                        if not remote_file.timestamp_reliable:
                            unreliable_timestamps += 1
                        if remote_file.mtime_utc is None:
                            missing_timestamps += 1

                    known = (
                        set()
                        if connection.full_local_reconciliation
                        else self.runs.successful_identities_for(
                            connection.id,
                            unique_batch,
                        )
                    )
                    planned_items: list[PlanItem] = []
                    candidate_indexes: list[int] = []
                    candidates: list[tuple[str, Path]] = []
                    for remote_file in unique_batch:
                        if connection.full_local_reconciliation:
                            status, reason = _classify_full_scan_eligibility(
                                remote_file,
                                connection=connection,
                                quiet_before=quiet_before,
                                includes=includes,
                                excludes=excludes,
                            )
                        else:
                            status, reason = _classify_file(
                                remote_file,
                                connection=connection,
                                window=window,
                                quiet_before=quiet_before,
                                includes=includes,
                                excludes=excludes,
                                known=known,
                            )
                            if status == PlanStatus.PLANNED:
                                known.add(remote_file.identity)
                        planned_items.append(
                            PlanItem(remote_file, status, reason)
                        )
                        if (
                            status == PlanStatus.PLANNED
                            or status == PlanStatus.LOCAL_MISSING
                        ):
                            try:
                                destination = build_destination(
                                    connection,
                                    remote_file,
                                    portable_root=self.paths.root,
                                    run_id=run_id,
                                    fallback_time=started_at,
                                )
                            except RecolectaError as exc:
                                if exc.error_type != ErrorType.PATH_INVALID:
                                    raise
                                planned_items[-1] = PlanItem(
                                    remote_file,
                                    PlanStatus.PATH_INVALID,
                                    str(exc),
                                )
                                continue
                            candidate_indexes.append(len(planned_items) - 1)
                            candidates.append(
                                (remote_file.remote_path, destination.path)
                            )

                    reserved_paths = self.runs.reserve_destinations(
                        connection_id=connection.id,
                        mapping_scope=mapping_scope,
                        candidates=candidates,
                    )
                    local_paths: dict[int, Path] = {}
                    for item_index, local_path in zip(
                        candidate_indexes,
                        reserved_paths,
                        strict=True,
                    ):
                        if isinstance(local_path, RecolectaError):
                            item = planned_items[item_index]
                            planned_items[item_index] = PlanItem(
                                item.file,
                                PlanStatus.PATH_INVALID,
                                str(local_path),
                            )
                            continue
                        local_paths[item_index] = local_path
                        if connection.full_local_reconciliation:
                            item = planned_items[item_index]
                            status, reason = _local_reconciliation_status(
                                local_path,
                                item.file,
                            )
                            planned_items[item_index] = PlanItem(
                                item.file,
                                status,
                                reason,
                            )

                    persistence_batch = []
                    persistence_indexes: list[int] = []
                    for item_index, item in enumerate(planned_items):
                        total_items += 1
                        counters[item.status.value] += 1
                        requires_download = _requires_download(item.status)
                        if requires_download:
                            planned_total += 1
                            file_size = item.file.size_bytes or 0
                            planned_bytes += file_size
                            if connection.full_local_reconciliation:
                                if item.file.size_bytes is None:
                                    unknown_sized_planned += 1
                                else:
                                    known_sized_planned += 1
                                    growth, staging_extra = (
                                        _estimated_reconciliation_space(
                                            destination_root,
                                            local_paths[item_index],
                                            item.file,
                                            connection,
                                        )
                                    )
                                    estimated_disk_growth += growth
                                    largest_staging_extra = max(
                                        largest_staging_extra,
                                        staging_extra,
                                    )
                            elif item.file.size_bytes is None:
                                unknown_sized_planned += 1
                            else:
                                estimated_disk_growth += (
                                    estimate_download_bytes(
                                        destination_root,
                                        item.file,
                                        connection=connection,
                                    )
                                )
                        retain_sample = len(plan_sample) < PLAN_SAMPLE_LIMIT
                        if retain_sample:
                            plan_sample.append(item)
                        if (
                            requires_download
                            or item.status == PlanStatus.PATH_INVALID
                            or retain_sample
                        ):
                            persistence_indexes.append(item_index)
                            persistence_batch.append(
                                (
                                    item.file,
                                    _persisted_plan_status(item.status),
                                    item.status.value,
                                    item.reason,
                                    (
                                        str(local_paths[item_index])
                                        if item_index in local_paths
                                        else None
                                    ),
                                    (
                                        ErrorType.PATH_INVALID.value
                                        if item.status
                                        == PlanStatus.PATH_INVALID
                                        else None
                                    ),
                                    (
                                        item.reason
                                        if item.status
                                        == PlanStatus.PATH_INVALID
                                        else ""
                                    ),
                                )
                            )
                    inserted_ids = self.runs.add_file_batch(
                        run_id=run_id,
                        connection_id=connection.id,
                        items=persistence_batch,
                    )
                    persisted_ids = dict(
                        zip(
                            persistence_indexes,
                            inserted_ids,
                            strict=True,
                        )
                    )
                    for item_index, item in enumerate(planned_items):
                        if total_items <= RUN_LOG_DETAIL_LIMIT:
                            run_log.write(
                                "file_planned",
                                run_file_id=persisted_ids.get(item_index),
                                remote_path=item.file.remote_path,
                                local_path=(
                                    str(local_paths[item_index])
                                    if item_index in local_paths
                                    else None
                                ),
                                size_bytes=item.file.size_bytes,
                                mtime_utc=(
                                    item.file.mtime_utc.isoformat()
                                    if item.file.mtime_utc is not None
                                    else None
                                ),
                                status=_persisted_plan_status(item.status),
                                plan_status=item.status.value,
                                reason=item.reason,
                            )

            for notice in listing_transport.last_listing_warnings:
                _append_warning(notices, notice)
            if unreliable_timestamps:
                _append_warning(
                    notices,
                    (
                        f"{unreliable_timestamps} archivo(s) tienen una fecha "
                        "remota de precisión limitada."
                    ),
                )
            if (
                connection.full_local_reconciliation
                and missing_timestamps
            ):
                _append_warning(
                    notices,
                    (
                        f"{missing_timestamps} archivo(s) no informaron fecha; "
                        "la comparación local usó el tamaño disponible."
                    ),
                )

            invalid_paths = counters[PlanStatus.PATH_INVALID.value]
            if invalid_paths:
                _append_warning(
                    warnings,
                    (
                        f"{invalid_paths} archivo(s) tenían una ruta remota "
                        "no permitida y fueron aislados."
                    ),
                )

            plan = DryRunPlan(
                connection_id=connection.id,
                window=window,
                items=tuple(plan_sample),
                warnings=tuple(warnings),
                notices=tuple(notices),
                total_items=total_items,
                counter_totals=counters,
                planned_total=planned_total,
                planned_bytes=planned_bytes,
                scan_mode=scan_mode,
            )
            self.runs.update_discovery(
                run_id,
                files_found=total_items,
                files_planned=planned_total,
                planned_bytes=planned_bytes,
                files_skipped=max(
                    0,
                    total_items
                    - planned_total
                    - counters[PlanStatus.PATH_INVALID.value],
                ),
                phase="downloading",
            )
            self.progress.set_totals(
                run_id,
                files_total=planned_total,
                total_size_bytes=planned_bytes,
                phase="downloading",
            )

            if cancel.is_set():
                cancelled_files = self.runs.cancel_unfinished(run_id)
                cancelled_counts = {
                    value.value: (
                        cancelled_files
                        if value == DownloadStatus.CANCELLED
                        else (
                            counters[PlanStatus.PATH_INVALID.value]
                            if value == DownloadStatus.FAILED
                            else 0
                        )
                    )
                    for value in DownloadStatus
                }
                status = "cancelled"
                self.runs.finish_run(
                    run_id,
                    status=status,
                    warnings=warnings,
                    notices=notices,
                )
                run_log.write(
                    "run_finished",
                    run_id=run_id,
                    status=status,
                    files_found=total_items,
                    files_planned=planned_total,
                    scan_mode=scan_mode,
                    warnings=warnings,
                    notices=notices,
                )
                if self.alerts is not None:
                    self._evaluate_alerts(run_id)
                return RunExecution(
                    connection.id,
                    run_id,
                    trigger,
                    status,
                    plan,
                    (),
                    cancelled_counts,
                    sum(cancelled_counts.values()) > 0,
                    discovery_scope=discovery_scope,
                )

            preflight_bytes = (
                estimated_disk_growth
                + DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES
                * min(
                    connection.max_parallel_files,
                    unknown_sized_planned,
                )
            )
            if connection.full_local_reconciliation:
                preflight_bytes += (
                    largest_staging_extra
                    * min(
                        connection.max_parallel_files,
                        known_sized_planned,
                    )
                )
            ensure_disk_space(
                destination_root,
                preflight_bytes,
                reserve_ratio=self.reserve_ratio,
            )
            engine = DownloadEngine(
                connection,
                portable_root=self.paths.root,
                transport_factory=_worker_transport_factory(
                    connection,
                    secret=secret,
                    known_hosts=self.paths.known_hosts,
                    listing_transport=listing_transport,
                ),
                throttle=self.throttle,
                minimum_spacing_s=self.minimum_spacing_s,
                reserve_ratio=self.reserve_ratio,
            )
            engine.__enter__()
            outcome_counts = {
                status.value: 0 for status in DownloadStatus
            }
            planning_failures = counters[PlanStatus.PATH_INVALID.value]
            outcome_counts[DownloadStatus.FAILED.value] = planning_failures
            outcome_sample: list[DownloadOutcome] = []
            detailed_logged = 0
            systemic_error: ErrorType | None = None
            systemic_signature: tuple[ErrorType, str] | None = None
            systemic_streak = 0
            circuit_error: ErrorType | None = None
            circuit_message = ""
            circuit_threshold = max(
                8,
                2 * min(connection.max_parallel_files, QUEUE_BATCH_MAX),
            )
            queue_limit = min(
                QUEUE_BATCH_MAX,
                max(1, connection.max_parallel_files * 2),
            )
            while not cancel.is_set():
                queue_rows = self.runs.claim_pending_batch(
                    run_id,
                    limit=queue_limit,
                )
                if not queue_rows:
                    break
                remote_files = tuple(
                    _remote_file_from_queue(row) for row in queue_rows
                )
                file_ids = {
                    remote_file.identity: int(row["id"])
                    for row, remote_file in zip(
                        queue_rows,
                        remote_files,
                        strict=True,
                    )
                }
                destination_paths = {
                    remote_file.identity: Path(str(row["local_path"]))
                    for row, remote_file in zip(
                        queue_rows,
                        remote_files,
                        strict=True,
                    )
                    if row.get("local_path")
                }
                required_batch_bytes = sum(
                    estimate_download_bytes(
                        destination_root,
                        remote_file,
                        connection=connection,
                    )
                    for remote_file in remote_files
                    if remote_file.size_bytes is not None
                ) + DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES * min(
                    connection.max_parallel_files,
                    sum(
                        remote_file.size_bytes is None
                        for remote_file in remote_files
                    ),
                )
                ensure_disk_space(
                    destination_root,
                    required_batch_bytes,
                    reserve_ratio=self.reserve_ratio,
                )
                self.progress.add_files(
                    run_id,
                    (
                        (file_ids[remote_file.identity], remote_file)
                        for remote_file in remote_files
                    ),
                )
                detail_slots = max(
                    0,
                    RUN_LOG_DETAIL_LIMIT - detailed_logged,
                )
                detailed_ids = {
                    int(row["id"]) for row in queue_rows[:detail_slots]
                }
                detailed_logged += len(detailed_ids)
                started_files: set[int] = set()
                logged_percent: dict[int, int] = {}
                event_lock = threading.Lock()

                def report_progress(
                    remote_file: RemoteFile,
                    bytes_done: int,
                    size_bytes: int | None,
                ) -> None:
                    file_id = file_ids[remote_file.identity]
                    if file_id in detailed_ids:
                        with event_lock:
                            if file_id not in started_files:
                                started_files.add(file_id)
                                run_log.write(
                                    "file_started",
                                    run_file_id=file_id,
                                    remote_path=remote_file.remote_path,
                                    size_bytes=size_bytes,
                                    worker=threading.current_thread().name,
                                )
                            if size_bytes and size_bytes > 0:
                                bucket = min(
                                    100,
                                    int(bytes_done * 100 / size_bytes)
                                    // 10
                                    * 10,
                                )
                                previous = logged_percent.get(file_id, 0)
                                if bucket >= 10 and bucket > previous:
                                    logged_percent[file_id] = bucket
                                    for threshold in range(
                                        previous + 10,
                                        bucket + 1,
                                        10,
                                    ):
                                        run_log.write(
                                            "file_progress",
                                            run_file_id=file_id,
                                            remote_path=(
                                                remote_file.remote_path
                                            ),
                                            bytes_done=bytes_done,
                                            size_bytes=size_bytes,
                                            percent=threshold,
                                        )
                    self.progress.update_file(
                        run_id,
                        file_id,
                        bytes_done,
                        size_bytes=size_bytes,
                        worker=threading.current_thread().name,
                    )

                def finish_progress(outcome: DownloadOutcome) -> None:
                    file_id = file_ids[outcome.remote_file.identity]
                    self.progress.finish_file(run_id, file_id, outcome)
                    if file_id in detailed_ids:
                        _log_outcome(run_log, file_id, outcome)

                batch_outcomes = engine.download_files(
                    remote_files,
                    run_id=run_id,
                    cancel_event=cancel,
                    on_progress=report_progress,
                    on_outcome=finish_progress,
                    destination_paths=destination_paths,
                    replace_existing=(
                        connection.full_local_reconciliation
                    ),
                    check_disk_space=False,
                )
                self.runs.record_download_outcomes_batch(
                    tuple(
                        (
                            file_ids[outcome.remote_file.identity],
                            outcome,
                        )
                        for outcome in batch_outcomes
                    )
                )
                for outcome in batch_outcomes:
                    outcome_counts[outcome.status.value] += 1
                    if len(outcome_sample) < OUTCOME_SAMPLE_LIMIT:
                        outcome_sample.append(outcome)
                    if (
                        outcome.status == DownloadStatus.FAILED
                        and outcome.error_type in SYSTEMIC_DOWNLOAD_ERRORS
                    ):
                        signature = _systemic_failure_signature(outcome)
                        if signature == systemic_signature:
                            systemic_streak += 1
                        else:
                            systemic_error = outcome.error_type
                            systemic_signature = signature
                            systemic_streak = 1
                    else:
                        systemic_error = None
                        systemic_signature = None
                        systemic_streak = 0
                run_log.write(
                    "download_batch_finished",
                    files=len(batch_outcomes),
                    first_run_file_id=int(queue_rows[0]["id"]),
                    last_run_file_id=int(queue_rows[-1]["id"]),
                    outcomes={
                        status.value: sum(
                            outcome.status == status
                            for outcome in batch_outcomes
                        )
                        for status in DownloadStatus
                    },
                )
                if (
                    not cancel.is_set()
                    and systemic_error is not None
                    and systemic_streak >= circuit_threshold
                ):
                    circuit_error = systemic_error
                    circuit_message = (
                        "La cola se detuvo de forma preventiva después de "
                        f"{systemic_streak} fallos consecutivos de tipo "
                        f"'{systemic_error.value}'. Revise la conexión antes "
                        "de reintentar."
                    )
                    terminalized = self.runs.fail_unfinished(
                        run_id,
                        error_type=systemic_error.value,
                        error_msg=circuit_message,
                    )
                    outcome_counts[DownloadStatus.FAILED.value] += terminalized
                    run_log.write(
                        "circuit_breaker_opened",
                        error_type=systemic_error.value,
                        consecutive_failures=systemic_streak,
                        files_terminalized=terminalized,
                        threshold=circuit_threshold,
                    )
                    break

            if cancel.is_set():
                outcome_counts[DownloadStatus.CANCELLED.value] += (
                    self.runs.cancel_unfinished(run_id)
                )
            status = _run_status_from_counts(
                outcome_counts,
                tuple(warnings),
                cancelled=cancel.is_set(),
            )
            run_error = (
                circuit_error
                if circuit_error is not None
                else (
                    ErrorType.PATH_INVALID
                    if planning_failures
                    else None
                )
            )
            run_error_message = (
                circuit_message
                if circuit_message
                else (
                    f"{planning_failures} archivo(s) tenían una ruta "
                    "remota no permitida y fueron aislados."
                    if planning_failures
                    else ""
                )
            )
            self.runs.finish_run(
                run_id,
                status=status,
                error_type=(
                    run_error.value if run_error is not None else None
                ),
                error_msg=run_error_message,
                warnings=warnings,
                notices=notices,
            )
            run_log.write(
                "run_finished",
                run_id=run_id,
                status=status,
                files_found=total_items,
                files_planned=planned_total,
                scan_mode=scan_mode,
                outcomes=outcome_counts,
                bytes_downloaded=sum(
                    outcome.bytes_done
                    for outcome in outcome_sample
                    if outcome.status == DownloadStatus.OK
                )
                if sum(outcome_counts.values()) == len(outcome_sample)
                else None,
                outcome_details_truncated=(
                    sum(outcome_counts.values()) > len(outcome_sample)
                ),
                plan_details_truncated=plan.items_truncated,
                warnings=warnings,
                notices=notices,
                error_type=(
                    run_error.value if run_error is not None else None
                ),
                error_msg=run_error_message,
            )
            if self.alerts is not None:
                self._evaluate_alerts(run_id)
            return RunExecution(
                connection.id,
                run_id,
                trigger,
                status,
                plan,
                tuple(outcome_sample),
                outcome_counts,
                sum(outcome_counts.values()) > len(outcome_sample),
                discovery_scope=discovery_scope,
            )
        except Exception as exc:
            error_type = classify_exception(exc)
            self.runs.fail_unfinished(
                run_id,
                error_type=error_type.value,
                error_msg=str(exc),
            )
            self.runs.finish_run(
                run_id,
                status="failed",
                error_type=error_type.value,
                error_msg=str(exc),
                warnings=warnings,
                notices=notices,
            )
            run_log.write(
                "run_finished",
                run_id=run_id,
                status="failed",
                error_type=error_type.value,
                error_msg=str(exc),
                warnings=warnings,
                notices=notices,
            )
            if self.alerts is not None:
                self._evaluate_alerts(run_id)
            raise
        finally:
            if engine is not None:
                engine.close()
            seen_paths.close()
            self.progress.finish_run(run_id)
            with self._state_lock:
                self._cancel_events.pop(run_id, None)

    def _record_preflight_failure(
        self,
        connection: Connection,
        *,
        trigger: str,
        started_at: datetime,
        window: TimeWindow,
        exc: Exception,
    ) -> int:
        assert connection.id is not None
        error_type = classify_exception(exc)
        discovery_scope = _effective_discovery_scope(connection)
        run_id = self.runs.start_run(
            connection_id=connection.id,
            trigger=trigger,
            window_start_utc=window.start_utc,
            window_end_utc=window.end_utc,
            started_at=started_at,
            scan_mode=(
                "full_local_reconciliation"
                if connection.full_local_reconciliation
                else "window"
            ),
            remote_paths=connection.remote_paths,
            recursive=bool(discovery_scope["recursive"]),
            max_depth=int(discovery_scope["max_depth"]),
        )
        self.runs.finish_run(
            run_id,
            status="failed",
            error_type=error_type.value,
            error_msg=str(exc),
        )
        try:
            run_log = self.run_logs.create(
                run_id=run_id,
                connection_name=connection.name,
                started_at=started_at,
            )
            run_log.write(
                "run_started",
                run_id=run_id,
                connection_id=connection.id,
                connection_name=connection.name,
                trigger=trigger,
                window_start_utc=window.start_utc.isoformat(),
                window_end_utc=window.end_utc.isoformat(),
            )
            run_log.write(
                "run_finished",
                run_id=run_id,
                status="failed",
                error_type=error_type.value,
                error_msg=str(exc),
            )
        except Exception:
            logger.exception(
                "No se pudo escribir el log de preflight de la corrida %s.",
                run_id,
            )
        self._evaluate_alerts(run_id)
        return run_id

    def _connection_lock(self, connection_id: int) -> threading.Lock:
        with self._state_lock:
            return self._connection_locks.setdefault(
                connection_id, threading.Lock()
            )

    def _evaluate_alerts(self, run_id: int) -> None:
        if self.alerts is None:
            return
        try:
            self.alerts.evaluate_run(run_id)
        except Exception:
            logger.exception(
                "No se pudieron evaluar las alertas de la corrida %s.",
                run_id,
            )


def calculate_window(
    connection: Connection,
    *,
    started_at: datetime,
    last_successful_end_utc: datetime | None = None,
    selected_date: date | None = None,
) -> TimeWindow:
    """Calculate a half-open UTC interval for a connection."""
    started_utc = _aware_utc(started_at, "started_at")
    zone = ZoneInfo(connection.timezone)

    if selected_date is not None:
        start_local = datetime.combine(selected_date, time.min, tzinfo=zone)
        end_local = datetime.combine(
            selected_date + timedelta(days=1), time.min, tzinfo=zone
        )
        return TimeWindow(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))

    if connection.window_mode == WindowMode.CALENDAR_DAY:
        today_local = started_utc.astimezone(zone).date()
        target = today_local - timedelta(days=1)
        start_local = datetime.combine(target, time.min, tzinfo=zone)
        end_local = datetime.combine(today_local, time.min, tzinfo=zone)
        return TimeWindow(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc))

    if connection.window_mode == WindowMode.ROLLING_HOURS:
        return TimeWindow(
            started_utc - timedelta(hours=connection.window_hours),
            started_utc,
        )

    if connection.window_mode == WindowMode.SINCE_LAST_RUN:
        if last_successful_end_utc is None:
            start = started_utc - timedelta(hours=connection.window_hours)
        else:
            last_end = _aware_utc(
                last_successful_end_utc, "last_successful_end_utc"
            )
            start = last_end - timedelta(minutes=connection.window_overlap_min)
        if start >= started_utc:
            raise ValueError(
                "La última corrida exitosa termina después del inicio actual."
            )
        return TimeWindow(start, started_utc)

    raise ValueError(f"Modo de ventana no soportado: {connection.window_mode}.")


def plan_listing(
    connection: Connection,
    listing: ListingResult,
    *,
    window: TimeWindow,
    started_at: datetime,
    successful_identities: set[tuple[str, str | None, int | None]] | None = None,
) -> DryRunPlan:
    """Apply time, quiet-period, glob, size, symlink, and dedupe rules."""
    started_utc = _aware_utc(started_at, "started_at")
    quiet_before = started_utc - timedelta(seconds=connection.quiet_period_s)
    known = set(successful_identities or ())
    includes, excludes = _split_globs(
        connection.include_globs, connection.exclude_globs
    )
    items: list[PlanItem] = []
    warnings: list[str] = []
    notices = list(listing.warnings)

    for remote_file in sorted(listing.files, key=lambda item: item.remote_path):
        status, reason = _classify_file(
            remote_file,
            connection=connection,
            window=window,
            quiet_before=quiet_before,
            includes=includes,
            excludes=excludes,
            known=known,
        )
        items.append(PlanItem(remote_file, status, reason))
        if status == PlanStatus.PLANNED:
            known.add(remote_file.identity)
        if not remote_file.timestamp_reliable:
            warning = (
                "La precisión temporal es limitada para "
                f"{remote_file.remote_path} ({remote_file.timestamp_source or 'fuente desconocida'})."
            )
            if warning not in notices:
                notices.append(warning)

    return DryRunPlan(
        connection_id=connection.id,
        window=window,
        items=tuple(items),
        warnings=tuple(warnings),
        notices=tuple(notices),
    )


def dry_run(
    connection: Connection,
    transport: Transport,
    *,
    started_at: datetime,
    database: Database | None = None,
    last_successful_end_utc: datetime | None = None,
    selected_date: date | None = None,
    portable_root: Path | None = None,
) -> DryRunPlan:
    """Incrementally plan a run without opening remote file contents."""
    normalized = connection.normalized()
    window = calculate_window(
        normalized,
        started_at=started_at,
        last_successful_end_utc=last_successful_end_utc,
        selected_date=selected_date,
    )
    root = (portable_root or Path.cwd()).resolve(strict=False)
    full_scan = normalized.full_local_reconciliation
    scan_mode = (
        "full_local_reconciliation" if full_scan else "window"
    )
    mapping_scope = _mapping_scope(normalized, root)
    counters = {status.value: 0 for status in PlanStatus}
    sample: list[PlanItem] = []
    warnings: list[str] = []
    notices: list[str] = []
    total_items = 0
    planned_total = 0
    planned_bytes = 0
    unreliable_timestamps = 0
    missing_timestamps = 0
    includes, excludes = _split_globs(
        normalized.include_globs,
        normalized.exclude_globs,
    )
    quiet_before = _aware_utc(started_at, "started_at") - timedelta(
        seconds=normalized.quiet_period_s
    )
    repository = RunRepository(database) if database is not None else None
    seen = sqlite3.connect("")
    seen.execute("PRAGMA temp_store = FILE")
    seen.execute("CREATE TABLE seen_paths (path_key TEXT PRIMARY KEY)")
    seen.execute(
        """
        CREATE TABLE dry_destination_reservations (
            remote_path TEXT NOT NULL,
            candidate_key BLOB NOT NULL,
            local_path TEXT NOT NULL,
            local_key BLOB NOT NULL UNIQUE,
            PRIMARY KEY (remote_path, candidate_key)
        )
        """
    )
    try:
        with _listing_iterator(
            transport,
            normalized.remote_paths,
            recursive=(True if full_scan else normalized.recursive),
            max_depth=(
                FULL_SCAN_MAX_DEPTH
                if full_scan
                else normalized.max_depth
            ),
            notices=notices,
        ) as discovered:
            for remote_batch in _iter_batches(
                discovered,
                DISCOVERY_BATCH_SIZE,
            ):
                unique_batch = tuple(
                    remote_file
                    for remote_file in remote_batch
                    if seen.execute(
                        """
                        INSERT OR IGNORE INTO seen_paths(path_key)
                        VALUES (?)
                        """,
                        (_plan_path_key(normalized, remote_file),),
                    ).rowcount
                )
                if not unique_batch:
                    continue
                known = (
                    repository.successful_identities_for(
                        normalized.id,
                        unique_batch,
                    )
                    if (
                        repository is not None
                        and normalized.id is not None
                        and not full_scan
                    )
                    else set()
                )
                batch_items: list[PlanItem] = []
                candidate_indexes: list[int] = []
                candidates: list[tuple[str, Path]] = []
                for remote_file in unique_batch:
                    if not remote_file.timestamp_reliable:
                        unreliable_timestamps += 1
                    if remote_file.mtime_utc is None:
                        missing_timestamps += 1
                    if full_scan:
                        status, reason = _classify_full_scan_eligibility(
                            remote_file,
                            connection=normalized,
                            quiet_before=quiet_before,
                            includes=includes,
                            excludes=excludes,
                        )
                    else:
                        status, reason = _classify_file(
                            remote_file,
                            connection=normalized,
                            window=window,
                            quiet_before=quiet_before,
                            includes=includes,
                            excludes=excludes,
                            known=known,
                        )
                        if status == PlanStatus.PLANNED:
                            known.add(remote_file.identity)
                    batch_items.append(PlanItem(remote_file, status, reason))
                    if status in {
                        PlanStatus.PLANNED,
                        PlanStatus.LOCAL_MISSING,
                    }:
                        try:
                            destination = build_destination(
                                normalized,
                                remote_file,
                                portable_root=root,
                                run_id=0,
                                fallback_time=started_at,
                            )
                        except RecolectaError as exc:
                            if exc.error_type != ErrorType.PATH_INVALID:
                                raise
                            batch_items[-1] = PlanItem(
                                remote_file,
                                PlanStatus.PATH_INVALID,
                                str(exc),
                            )
                        else:
                            candidate_indexes.append(len(batch_items) - 1)
                            candidates.append(
                                (remote_file.remote_path, destination.path)
                            )

                if candidates:
                    reserved_paths = _reserve_transient_destinations(
                        seen,
                        candidates,
                        persistent_database=database,
                        connection_id=normalized.id,
                        mapping_scope=mapping_scope,
                    )
                    for item_index, local_path in zip(
                        candidate_indexes,
                        reserved_paths,
                        strict=True,
                    ):
                        item = batch_items[item_index]
                        if isinstance(local_path, RecolectaError):
                            batch_items[item_index] = PlanItem(
                                item.file,
                                PlanStatus.PATH_INVALID,
                                str(local_path),
                            )
                            continue
                        if full_scan:
                            status, reason = _local_reconciliation_status(
                                local_path,
                                item.file,
                            )
                            batch_items[item_index] = PlanItem(
                                item.file,
                                status,
                                reason,
                            )

                for item in batch_items:
                    total_items += 1
                    counters[item.status.value] += 1
                    if _requires_download(item.status):
                        planned_total += 1
                        planned_bytes += item.file.size_bytes or 0
                    if len(sample) < PLAN_SAMPLE_LIMIT:
                        sample.append(item)

        for notice in transport.last_listing_warnings:
            _append_warning(notices, notice)
        if unreliable_timestamps:
            _append_warning(
                notices,
                (
                    f"{unreliable_timestamps} archivo(s) tienen una fecha "
                    "remota de precisión limitada."
                ),
            )
        if full_scan and missing_timestamps:
            _append_warning(
                notices,
                (
                    f"{missing_timestamps} archivo(s) no informaron fecha; "
                    "la comparación local usó el tamaño disponible."
                ),
            )
        invalid_paths = counters[PlanStatus.PATH_INVALID.value]
        if invalid_paths:
            _append_warning(
                warnings,
                (
                    f"{invalid_paths} archivo(s) tenían una ruta remota "
                    "no permitida; se aislarán sin detener los demás."
                ),
            )
    finally:
        seen.close()

    return DryRunPlan(
        connection_id=normalized.id,
        window=window,
        items=tuple(sample),
        warnings=tuple(warnings),
        notices=tuple(notices),
        total_items=total_items,
        counter_totals=counters,
        planned_total=planned_total,
        planned_bytes=planned_bytes,
        scan_mode=scan_mode,
    )


def load_successful_identities(
    database: Database, connection_id: int
) -> set[tuple[str, str | None, int | None]]:
    """Load the logical identities already downloaded successfully."""
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT remote_path, mtime_utc, size_bytes
            FROM run_files
            WHERE connection_id = ? AND status = 'ok'
            """,
            (connection_id,),
        ).fetchall()
    identities: set[tuple[str, str | None, int | None]] = set()
    for row in rows:
        timestamp = _canonical_timestamp(row["mtime_utc"])
        identities.add((row["remote_path"], timestamp, row["size_bytes"]))
    return identities


def _classify_file(
    remote_file: RemoteFile,
    *,
    connection: Connection,
    window: TimeWindow,
    quiet_before: datetime,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    known: set[tuple[str, str | None, int | None]],
) -> tuple[PlanStatus, str]:
    if remote_file.is_symlink:
        return PlanStatus.SYMLINK, "Los enlaces simbólicos no se siguen."
    if remote_file.mtime_utc is None:
        return PlanStatus.TIMESTAMP_MISSING, "El servidor no informó la fecha."
    if not window.start_utc <= remote_file.mtime_utc < window.end_utc:
        return PlanStatus.OUTSIDE_WINDOW, "El archivo está fuera de la ventana."
    if remote_file.mtime_utc > quiet_before:
        return PlanStatus.QUIET_PERIOD, "El archivo puede estar todavía en escritura."
    if includes and not _matches_any(remote_file, includes):
        return PlanStatus.INCLUDE_FILTER, "No coincide con los filtros de inclusión."
    if excludes and _matches_any(remote_file, excludes, match_path_parts=True):
        return PlanStatus.EXCLUDE_FILTER, "Coincide con un filtro de exclusión."
    if (
        connection.min_size_bytes is not None
        and (remote_file.size_bytes is None or remote_file.size_bytes < connection.min_size_bytes)
    ):
        return PlanStatus.SIZE_FILTER, "Es menor que el tamaño mínimo."
    if (
        connection.max_size_bytes is not None
        and (remote_file.size_bytes is None or remote_file.size_bytes > connection.max_size_bytes)
    ):
        return PlanStatus.SIZE_FILTER, "Supera el tamaño máximo."
    if remote_file.identity in known:
        return PlanStatus.DUPLICATE, "Ya fue descargado correctamente."
    return PlanStatus.PLANNED, ""


def _classify_full_scan_eligibility(
    remote_file: RemoteFile,
    *,
    connection: Connection,
    quiet_before: datetime,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
) -> tuple[PlanStatus, str]:
    """Apply every configured filter except window and historical dedupe."""
    if remote_file.is_symlink:
        return PlanStatus.SYMLINK, "Los enlaces simbólicos no se siguen."
    if (
        remote_file.mtime_utc is not None
        and remote_file.mtime_utc > quiet_before
    ):
        return (
            PlanStatus.QUIET_PERIOD,
            "El archivo puede estar todavía en escritura.",
        )
    if includes and not _matches_any(remote_file, includes):
        return (
            PlanStatus.INCLUDE_FILTER,
            "No coincide con los filtros de inclusión.",
        )
    if excludes and _matches_any(
        remote_file,
        excludes,
        match_path_parts=True,
    ):
        return (
            PlanStatus.EXCLUDE_FILTER,
            "Coincide con un filtro de exclusión.",
        )
    if (
        connection.min_size_bytes is not None
        and (
            remote_file.size_bytes is None
            or remote_file.size_bytes < connection.min_size_bytes
        )
    ):
        return PlanStatus.SIZE_FILTER, "Es menor que el tamaño mínimo."
    if (
        connection.max_size_bytes is not None
        and (
            remote_file.size_bytes is None
            or remote_file.size_bytes > connection.max_size_bytes
        )
    ):
        return PlanStatus.SIZE_FILTER, "Supera el tamaño máximo."
    # This is an eligibility marker; the local stat below selects the exact
    # reconciliation state before anything is persisted.
    return PlanStatus.LOCAL_MISSING, ""


def _local_reconciliation_status(
    local_path: Path,
    remote_file: RemoteFile,
) -> tuple[PlanStatus, str]:
    """Compare metadata without reading or hashing millions of local files."""
    try:
        if not local_path.exists():
            return (
                PlanStatus.LOCAL_MISSING,
                "El archivo no existe en la carpeta local.",
            )
        if local_path.is_symlink() or not local_path.is_file():
            return (
                PlanStatus.LOCAL_DIFFERENT,
                "La ruta local existe, pero no es un archivo regular.",
            )
        local_stat = local_path.stat()
    except OSError as exc:
        return (
            PlanStatus.LOCAL_DIFFERENT,
            f"No fue posible validar el archivo local: {exc}.",
        )
    if (
        remote_file.size_bytes is None
        and remote_file.mtime_utc is None
    ):
        return (
            PlanStatus.LOCAL_DIFFERENT,
            (
                "El remoto no informó tamaño ni fecha; no es posible "
                "confirmar que el archivo local sea equivalente."
            ),
        )
    if (
        remote_file.size_bytes is not None
        and local_stat.st_size != remote_file.size_bytes
    ):
        return (
            PlanStatus.LOCAL_DIFFERENT,
            (
                f"El tamaño local ({local_stat.st_size}) no coincide con el "
                f"remoto ({remote_file.size_bytes})."
            ),
        )
    if (
        remote_file.mtime_utc is not None
        and abs(
            local_stat.st_mtime - remote_file.mtime_utc.timestamp()
        )
        > LOCAL_MTIME_TOLERANCE_S
    ):
        return (
            PlanStatus.LOCAL_DIFFERENT,
            "La fecha de modificación local no coincide con la remota.",
        )
    return (
        PlanStatus.LOCAL_PRESENT,
        "El archivo local existe y coincide con los metadatos remotos.",
    )


def _estimated_reconciliation_space(
    destination_root: Path,
    local_path: Path,
    remote_file: RemoteFile,
    connection: Connection,
) -> tuple[int, int]:
    """Return final net growth and transient staging growth still required."""
    remaining = estimate_download_bytes(
        destination_root,
        remote_file,
        connection=connection,
    )
    try:
        local_size = (
            local_path.stat().st_size
            if local_path.is_file() and not local_path.is_symlink()
            else 0
        )
    except OSError:
        local_size = 0
    net_growth = max(0, remaining - local_size)
    return net_growth, remaining - net_growth


def _requires_download(status: PlanStatus) -> bool:
    return status in {
        PlanStatus.PLANNED,
        PlanStatus.LOCAL_MISSING,
        PlanStatus.LOCAL_DIFFERENT,
    }


def _persisted_plan_status(status: PlanStatus) -> str:
    if _requires_download(status):
        return "pending"
    if status == PlanStatus.PATH_INVALID:
        return "failed"
    if status == PlanStatus.DUPLICATE:
        return "duplicate"
    return "skipped"


@contextmanager
def _listing_iterator(
    transport: Transport,
    remote_paths: tuple[str, ...],
    *,
    recursive: bool,
    max_depth: int,
    notices: list[str] | None = None,
) -> Iterator[Iterator[RemoteFile]]:
    """Close a partially consumed listing before its transport session."""
    discovered: Iterator[RemoteFile] | None = None
    try:
        with transport:
            try:
                discovered = transport.iter_files(
                    remote_paths,
                    recursive=recursive,
                    max_depth=max_depth,
                )
                # Protocol adapters own exception classification.  Keeping
                # the original exception here avoids presenting application
                # or parser bugs as a verified remote protocol failure.
                yield discovered
            finally:
                if discovered is not None:
                    close_discovered = getattr(discovered, "close", None)
                    if callable(close_discovered):
                        try:
                            close_discovered()
                        except Exception:
                            # A partially consumed network listing can report
                            # an abort while its generator is being closed.
                            # The enclosing transport is closed next, so this
                            # cleanup error must not turn cancellation (or a
                            # primary planning error) into a failed run.
                            logger.warning(
                                "El iterador remoto informó un error al "
                                "cerrarse; la sesión se cerrará a "
                                "continuación.",
                                exc_info=True,
                            )
    finally:
        if notices is not None:
            for notice in transport.last_listing_warnings:
                _append_warning(notices, notice)


def _worker_transport_factory(
    connection: Connection,
    *,
    secret: str | None,
    known_hosts: Path,
    listing_transport: Transport,
) -> Callable[[], Transport]:
    """Create workers with the FTP command encoding learned at discovery."""
    ftp_command_encoding = None
    if connection.protocol in {Protocol.FTP, Protocol.FTPS}:
        candidate = getattr(listing_transport, "command_encoding", None)
        if isinstance(candidate, str):
            ftp_command_encoding = candidate

    if ftp_command_encoding is None:
        return lambda: create_transport(
            connection,
            secret=secret,
            known_hosts=known_hosts,
        )
    return lambda: create_transport(
        connection,
        secret=secret,
        known_hosts=known_hosts,
        ftp_command_encoding=ftp_command_encoding,
    )


def _iter_batches(
    values: Iterable[RemoteFile],
    size: int,
    *,
    stop_event: threading.Event | None = None,
) -> Iterator[tuple[RemoteFile, ...]]:
    if size < 1:
        raise ValueError("El tamaño del lote debe ser positivo.")
    iterator = iter(values)
    while True:
        batch: list[RemoteFile] = []
        for _ in range(size):
            if stop_event is not None and stop_event.is_set():
                break
            try:
                batch.append(next(iterator))
            except StopIteration:
                break
        if not batch:
            return
        yield tuple(batch)


def _reserve_transient_destinations(
    transient: sqlite3.Connection,
    candidates: Sequence[tuple[str, Path]],
    *,
    persistent_database: Database | None,
    connection_id: int | None,
    mapping_scope: str,
) -> tuple[Path | RecolectaError, ...]:
    """Mirror persistent destination collision handling for a dry-run."""
    if persistent_database is not None and connection_id is not None:
        with persistent_database.connect() as persistent:
            return _reserve_transient_destination_rows(
                transient,
                candidates,
                persistent=persistent,
                connection_id=connection_id,
                mapping_scope=mapping_scope,
            )
    return _reserve_transient_destination_rows(
        transient,
        candidates,
        persistent=None,
        connection_id=connection_id,
        mapping_scope=mapping_scope,
    )


def _reserve_transient_destination_rows(
    transient: sqlite3.Connection,
    candidates: Sequence[tuple[str, Path]],
    *,
    persistent: sqlite3.Connection | None,
    connection_id: int | None,
    mapping_scope: str,
) -> tuple[Path | RecolectaError, ...]:
    reserved: list[Path | RecolectaError] = []
    for remote_path, candidate in candidates:
        candidate_key = local_path_key(candidate)
        existing = transient.execute(
            """
            SELECT local_path
            FROM dry_destination_reservations
            WHERE remote_path = ? AND candidate_key = ?
            """,
            (remote_path, candidate_key),
        ).fetchone()
        if existing is not None:
            reserved.append(Path(existing[0]))
            continue

        if persistent is not None and connection_id is not None:
            existing = persistent.execute(
                """
                SELECT local_path
                FROM destination_reservations
                WHERE connection_id = ? AND mapping_scope = ?
                  AND remote_path = ? AND candidate_key = ?
                """,
                (
                    connection_id,
                    mapping_scope,
                    remote_path,
                    candidate_key,
                ),
            ).fetchone()
            if existing is not None:
                selected = Path(existing["local_path"])
                transient.execute(
                    """
                    INSERT OR IGNORE INTO dry_destination_reservations(
                        remote_path, candidate_key, local_path, local_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        remote_path,
                        candidate_key,
                        str(selected),
                        local_path_key(selected),
                    ),
                )
                reserved.append(selected)
                continue

        selected = candidate
        suffix = hashlib.sha256(
            remote_path.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:10]
        counter = 1
        while True:
            selected_key = local_path_key(selected)
            transient_owner = transient.execute(
                """
                SELECT remote_path, local_path
                FROM dry_destination_reservations
                WHERE local_key = ?
                """,
                (selected_key,),
            ).fetchone()
            persistent_owner = (
                persistent.execute(
                    """
                    SELECT connection_id, remote_path, local_path
                    FROM destination_reservations
                    WHERE local_key = ?
                    """,
                    (selected_key,),
                ).fetchone()
                if persistent is not None
                else None
            )
            if transient_owner is None and persistent_owner is None:
                transient.execute(
                    """
                    INSERT INTO dry_destination_reservations(
                        remote_path, candidate_key, local_path, local_key
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (remote_path, candidate_key, str(selected), selected_key),
                )
                reserved.append(selected)
                break
            if (
                transient_owner is not None
                and transient_owner[0] == remote_path
            ):
                reserved.append(Path(transient_owner[1]))
                break
            if (
                persistent_owner is not None
                and connection_id is not None
                and int(persistent_owner["connection_id"]) == connection_id
                and persistent_owner["remote_path"] == remote_path
            ):
                reserved.append(Path(persistent_owner["local_path"]))
                break
            marker = f"__{suffix}"
            if counter > 1:
                marker += f"_{counter}"
            try:
                selected = collision_path(candidate, marker)
            except RecolectaError as exc:
                reserved.append(exc)
                break
            counter += 1
    return tuple(reserved)


def _mapping_scope(connection: Connection, portable_root: Path) -> str:
    root = resolve_destination_root(connection, portable_root)
    values = (
        str(root),
        connection.dest_template,
        connection.protocol.value,
        connection.host.casefold(),
        *connection.remote_paths,
    )
    payload = "\x00".join(values).encode(
        "utf-8",
        errors="surrogatepass",
    )
    return hashlib.sha256(payload).hexdigest()


def _plan_path_key(
    connection: Connection,
    remote_file: RemoteFile,
) -> str:
    path = remote_file.remote_path.replace("\\", "/")
    if connection.protocol == Protocol.SMB:
        path = path.casefold()
    payload = path.encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _remote_file_from_queue(row: dict[str, object]) -> RemoteFile:
    timestamp_value = row.get("mtime_utc")
    modified = (
        datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
        if timestamp_value
        else None
    )
    return RemoteFile(
        remote_path=str(row["remote_path"]),
        size_bytes=(
            int(row["size_bytes"])
            if row.get("size_bytes") is not None
            else None
        ),
        mtime_utc=modified,
        timestamp_reliable=bool(row.get("timestamp_reliable", 0)),
        timestamp_source=str(row.get("timestamp_source") or ""),
    )


def _append_warning(warnings: list[str], value: str) -> None:
    if value and value not in warnings:
        warnings.append(value)


def _split_globs(
    include_globs: tuple[str, ...], exclude_globs: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    includes: list[str] = []
    excludes = list(exclude_globs)
    for pattern in include_globs:
        if pattern.startswith("!"):
            if pattern[1:]:
                excludes.append(pattern[1:])
        else:
            includes.append(pattern)
    return tuple(includes), tuple(excludes)


def _matches_any(
    remote_file: RemoteFile,
    patterns: tuple[str, ...],
    *,
    match_path_parts: bool = False,
) -> bool:
    path = remote_file.remote_path.replace("\\", "/")
    name = PurePosixPath(path).name
    candidates = [name, path]
    if match_path_parts:
        candidates.extend(part for part in PurePosixPath(path).parts if part != "/")
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} debe incluir zona horaria.")
    return value.astimezone(timezone.utc)


def _canonical_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed, "mtime_utc").isoformat(timespec="seconds")


def _systemic_failure_signature(
    outcome: DownloadOutcome,
) -> tuple[ErrorType, str]:
    """Group one root cause while excluding per-file diagnostic values."""
    error_type = outcome.error_type
    if error_type is None:
        raise ValueError("Un fallo sistémico debe incluir error_type.")
    message = outcome.error_msg.casefold().strip()
    dynamic_values = (
        outcome.remote_file.remote_path,
        str(outcome.local_path) if outcome.local_path is not None else "",
        (
            outcome.remote_file.mtime_utc.isoformat()
            if outcome.remote_file.mtime_utc is not None
            else ""
        ),
    )
    for value in dynamic_values:
        if len(value) > 1:
            message = message.replace(value.casefold(), "<item>")
    dynamic_numbers = {
        outcome.remote_file.size_bytes,
        outcome.bytes_done,
        outcome.resumed_from,
    }
    for value in sorted(
        (number for number in dynamic_numbers if number is not None),
        key=lambda number: len(str(number)),
        reverse=True,
    ):
        message = re.sub(
            rf"(?<!\d){re.escape(str(value))}(?!\d)",
            "<value>",
            message,
        )
    message = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
        "<id>",
        message,
    )
    message = " ".join(message.split())
    return error_type, message


def _log_outcome(
    run_log: RunEventLog,
    run_file_id: int,
    outcome: DownloadOutcome,
) -> None:
    event = (
        "file_failed"
        if outcome.status == DownloadStatus.FAILED
        else "file_done"
    )
    run_log.write(
        event,
        run_file_id=run_file_id,
        remote_path=outcome.remote_file.remote_path,
        local_path=str(outcome.local_path) if outcome.local_path else None,
        status=outcome.status.value,
        bytes_done=outcome.bytes_done,
        attempts=outcome.attempts,
        duration_s=outcome.duration_s,
        sha256=outcome.sha256,
        error_type=(
            outcome.error_type.value if outcome.error_type else None
        ),
        error_msg=outcome.error_msg,
        resumed_from=outcome.resumed_from,
        resume_supported=outcome.resume_supported,
    )


def _run_status(
    outcomes: tuple[DownloadOutcome, ...], warnings: tuple[str, ...]
) -> str:
    counts = {
        status.value: sum(
            outcome.status == status for outcome in outcomes
        )
        for status in DownloadStatus
    }
    return _run_status_from_counts(
        counts,
        warnings,
        cancelled=bool(counts[DownloadStatus.CANCELLED.value]),
    )


def _run_status_from_counts(
    counts: dict[str, int],
    warnings: tuple[str, ...],
    *,
    cancelled: bool,
) -> str:
    if cancelled:
        return "cancelled"
    failures = counts.get(DownloadStatus.FAILED.value, 0)
    successes = counts.get(DownloadStatus.OK.value, 0)
    if failures and not successes:
        return "failed"
    if failures or warnings:
        return "partial"
    return "ok"
