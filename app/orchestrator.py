"""Time-window calculation and side-effect-free run planning."""

from __future__ import annotations

import fnmatch
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import AppPaths
from app.db import ConnectionRepository, Database, RunRepository
from app.downloader import DownloadEngine, DownloadOutcome, DownloadStatus
from app.errors import ErrorType, HarvesterError, classify_exception
from app.models import Connection, WindowMode
from app.progress import ProgressRegistry
from app.throttle import ThrottleManager
from app.transports import create_transport
from app.transports.base import ListingResult, RemoteFile, Transport

if TYPE_CHECKING:
    from collections.abc import Iterable


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

    @property
    def files_to_download(self) -> tuple[RemoteFile, ...]:
        return tuple(
            item.file for item in self.items if item.status == PlanStatus.PLANNED
        )

    @property
    def is_partial(self) -> bool:
        return bool(self.warnings)

    @property
    def counters(self) -> dict[str, int]:
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

    def summary(self) -> dict[str, object]:
        return {
            "connection_id": self.connection_id,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": self.status,
            "window_start_utc": self.plan.window.start_utc.isoformat(),
            "window_end_utc": self.plan.window.end_utc.isoformat(),
            "files_planned": len(self.plan.files_to_download),
            "warnings": list(self.plan.warnings),
            "outcomes": {
                status.value: sum(
                    outcome.status == status for outcome in self.outcomes
                )
                for status in DownloadStatus
            },
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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.connections = connections
        self.runs = RunRepository(database)
        self.paths = paths
        self.throttle = throttle or ThrottleManager(global_parallelism=4)
        self.progress = progress or ProgressRegistry(
            persist_progress=self.runs.update_file_progress
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._state_lock = threading.Lock()
        self._connection_locks: dict[int, threading.Lock] = {}
        self._cancel_events: dict[int, threading.Event] = {}

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
        connection = self.connections.get(connection_id)
        if not connection.enabled and not dry_run_only:
            raise HarvesterError(
                ErrorType.INTERRUPTED,
                f"La conexión {connection.name} está en pausa.",
            )
        lock = self._connection_lock(connection_id)
        if not lock.acquire(blocking=False):
            raise HarvesterError(
                ErrorType.INTERRUPTED,
                f"Ya hay una corrida activa para {connection.name}.",
            )
        try:
            actual_started = started_at or self.now()
            reference = window_reference_at or actual_started
            if actual_started.tzinfo is None or reference.tzinfo is None:
                raise ValueError("started_at debe incluir zona horaria.")
            secret = self.connections.get_secret(connection_id)
            last_end = self.runs.last_successful_end(connection_id)
            listing_transport = create_transport(
                connection,
                secret=secret,
                known_hosts=self.paths.known_hosts,
            )
            plan = dry_run(
                connection,
                listing_transport,
                started_at=reference,
                database=self.database,
                last_successful_end_utc=last_end,
                selected_date=selected_date,
            )
            if dry_run_only:
                return RunExecution(
                    connection_id,
                    None,
                    trigger,
                    "dry_run",
                    plan,
                )
            if trigger in {"schedule", "catchup"} and self.runs.has_successful_window(
                connection_id,
                window_start_utc=plan.window.start_utc,
                window_end_utc=plan.window.end_utc,
            ):
                return RunExecution(
                    connection_id,
                    None,
                    trigger,
                    "already_completed",
                    plan,
                )
            return self._execute_plan(
                connection,
                secret=secret,
                trigger=trigger,
                started_at=actual_started,
                plan=plan,
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

    def _execute_plan(
        self,
        connection: Connection,
        *,
        secret: str | None,
        trigger: str,
        started_at: datetime,
        plan: DryRunPlan,
    ) -> RunExecution:
        assert connection.id is not None
        run_id = self.runs.start_run(
            connection_id=connection.id,
            trigger=trigger,
            window_start_utc=plan.window.start_utc,
            window_end_utc=plan.window.end_utc,
            started_at=started_at,
        )
        file_ids: dict[
            tuple[str, str | None, int | None], int
        ] = {}
        for item in plan.items:
            if item.status == PlanStatus.PLANNED:
                persisted_status = "pending"
            elif item.status == PlanStatus.DUPLICATE:
                persisted_status = "duplicate"
            else:
                persisted_status = "skipped"
            file_id = self.runs.add_file(
                run_id=run_id,
                connection_id=connection.id,
                remote_file=item.file,
                status=persisted_status,
            )
            if item.status == PlanStatus.PLANNED:
                file_ids[item.file.identity] = file_id
                self.runs.mark_downloading(
                    file_id, attempts=0, bytes_done=0
                )

        self.progress.start_run(
            run_id=run_id,
            connection_id=connection.id,
            connection_name=connection.name,
            trigger=trigger,
            files=(
                (file_ids[item.file.identity], item.file)
                for item in plan.items
                if item.status == PlanStatus.PLANNED
            ),
        )
        cancel = threading.Event()
        with self._state_lock:
            self._cancel_events[run_id] = cancel
        try:
            engine = DownloadEngine(
                connection,
                portable_root=self.paths.root,
                transport_factory=lambda: create_transport(
                    connection,
                    secret=secret,
                    known_hosts=self.paths.known_hosts,
                ),
                throttle=self.throttle,
            )

            def persist(outcome: DownloadOutcome) -> None:
                file_id = file_ids[outcome.remote_file.identity]
                self.runs.record_download_outcome(file_id, outcome)
                self.progress.finish_file(run_id, file_id, outcome)

            def report_progress(
                remote_file: RemoteFile,
                bytes_done: int,
                size_bytes: int | None,
            ) -> None:
                self.progress.update_file(
                    run_id,
                    file_ids[remote_file.identity],
                    bytes_done,
                    size_bytes=size_bytes,
                    worker=threading.current_thread().name,
                )

            outcomes = engine.download_files(
                plan.files_to_download,
                run_id=run_id,
                cancel_event=cancel,
                on_progress=report_progress,
                on_outcome=persist,
            )
            status = _run_status(outcomes, plan.warnings)
            self.runs.finish_run(run_id, status=status)
            return RunExecution(
                connection.id,
                run_id,
                trigger,
                status,
                plan,
                outcomes,
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
            )
            raise
        finally:
            self.progress.finish_run(run_id)
            with self._state_lock:
                self._cancel_events.pop(run_id, None)

    def _connection_lock(self, connection_id: int) -> threading.Lock:
        with self._state_lock:
            return self._connection_locks.setdefault(
                connection_id, threading.Lock()
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
    warnings = list(listing.warnings)

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
            if warning not in warnings:
                warnings.append(warning)

    return DryRunPlan(
        connection_id=connection.id,
        window=window,
        items=tuple(items),
        warnings=tuple(warnings),
    )


def dry_run(
    connection: Connection,
    transport: Transport,
    *,
    started_at: datetime,
    database: Database | None = None,
    last_successful_end_utc: datetime | None = None,
    selected_date: date | None = None,
) -> DryRunPlan:
    """List and plan a run without opening any remote file for reading."""
    normalized = connection.normalized()
    window = calculate_window(
        normalized,
        started_at=started_at,
        last_successful_end_utc=last_successful_end_utc,
        selected_date=selected_date,
    )
    with transport:
        listing = transport.list_files(
            normalized.remote_paths,
            recursive=normalized.recursive,
            max_depth=normalized.max_depth,
        )
    identities = (
        load_successful_identities(database, normalized.id)
        if database is not None and normalized.id is not None
        else set()
    )
    return plan_listing(
        normalized,
        listing,
        window=window,
        started_at=started_at,
        successful_identities=identities,
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


def _run_status(
    outcomes: tuple[DownloadOutcome, ...], warnings: tuple[str, ...]
) -> str:
    if any(outcome.status == DownloadStatus.CANCELLED for outcome in outcomes):
        return "cancelled"
    failures = sum(
        outcome.status == DownloadStatus.FAILED for outcome in outcomes
    )
    successes = sum(
        outcome.status == DownloadStatus.OK for outcome in outcomes
    )
    if failures and not successes:
        return "failed"
    if failures or warnings:
        return "partial"
    return "ok"
