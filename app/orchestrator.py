"""Time-window calculation and side-effect-free run planning."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.db import Database
from app.models import Connection, WindowMode
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
