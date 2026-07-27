"""Thread-safe in-memory progress snapshots for the local dashboard."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.downloader import DownloadOutcome
from app.transports.base import RemoteFile


@dataclass
class _FileProgress:
    run_file_id: int
    remote_path: str
    size_bytes: int | None
    bytes_done: int
    status: str
    worker: str | None
    started_mono: float | None
    updated_mono: float
    previous_bytes: int
    previous_mono: float
    instant_bps: float
    last_persisted_mono: float

    def snapshot(self, now: float) -> dict[str, Any]:
        elapsed = (
            max(0.0, now - self.started_mono)
            if self.started_mono is not None
            else 0.0
        )
        average = self.bytes_done / elapsed if elapsed > 0 else 0.0
        eta = _eta(self.size_bytes, self.bytes_done, average)
        return {
            "run_file_id": self.run_file_id,
            "remote_path": self.remote_path,
            "size_bytes": self.size_bytes,
            "bytes_done": self.bytes_done,
            "status": self.status,
            "worker": self.worker,
            "instant_bps": round(self.instant_bps, 2),
            "average_bps": round(average, 2),
            "eta_s": eta,
            "percent": _percent(self.size_bytes, self.bytes_done),
        }


@dataclass
class _RunProgress:
    run_id: int
    connection_id: int
    connection_name: str
    trigger: str
    started_at: str
    started_mono: float
    cancel_requested: bool
    files: dict[int, _FileProgress]


class ProgressRegistry:
    """Maintain live progress and throttle per-file database checkpoints."""

    def __init__(
        self,
        *,
        persist_progress: Callable[[int, int], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        persist_interval_s: float = 1.0,
    ) -> None:
        if persist_interval_s <= 0:
            raise ValueError("El intervalo de persistencia debe ser positivo.")
        self._persist_progress = persist_progress
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._persist_interval_s = persist_interval_s
        self._lock = threading.RLock()
        self._runs: dict[int, _RunProgress] = {}

    def start_run(
        self,
        *,
        run_id: int,
        connection_id: int,
        connection_name: str,
        trigger: str,
        files: Iterable[tuple[int, RemoteFile]],
    ) -> None:
        now = self._monotonic()
        progress_files = {
            file_id: _FileProgress(
                run_file_id=file_id,
                remote_path=remote_file.remote_path,
                size_bytes=remote_file.size_bytes,
                bytes_done=0,
                status="pending",
                worker=None,
                started_mono=None,
                updated_mono=now,
                previous_bytes=0,
                previous_mono=now,
                instant_bps=0.0,
                last_persisted_mono=now,
            )
            for file_id, remote_file in files
        }
        started = self._wall_clock()
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        with self._lock:
            self._runs[run_id] = _RunProgress(
                run_id=run_id,
                connection_id=connection_id,
                connection_name=connection_name,
                trigger=trigger,
                started_at=started.astimezone(timezone.utc).isoformat(),
                started_mono=now,
                cancel_requested=False,
                files=progress_files,
            )

    def update_file(
        self,
        run_id: int,
        run_file_id: int,
        bytes_done: int,
        *,
        size_bytes: int | None = None,
        worker: str | None = None,
    ) -> None:
        now = self._monotonic()
        persist: tuple[int, int] | None = None
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run_file_id not in run.files:
                return
            file = run.files[run_file_id]
            normalized_bytes = max(0, bytes_done)
            elapsed = now - file.previous_mono
            if normalized_bytes < file.bytes_done:
                file.instant_bps = 0.0
            elif elapsed > 0:
                file.instant_bps = (
                    normalized_bytes - file.previous_bytes
                ) / elapsed
            file.previous_bytes = normalized_bytes
            file.previous_mono = now
            file.bytes_done = normalized_bytes
            file.updated_mono = now
            file.status = "downloading"
            file.worker = worker or file.worker
            if size_bytes is not None:
                file.size_bytes = size_bytes
            if file.started_mono is None:
                file.started_mono = now
            if (
                self._persist_progress is not None
                and now - file.last_persisted_mono >= self._persist_interval_s
            ):
                file.last_persisted_mono = now
                persist = (run_file_id, normalized_bytes)
        if persist is not None:
            self._persist_progress(*persist)

    def finish_file(
        self, run_id: int, run_file_id: int, outcome: DownloadOutcome
    ) -> None:
        now = self._monotonic()
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run_file_id not in run.files:
                return
            file = run.files[run_file_id]
            file.bytes_done = max(0, outcome.bytes_done)
            file.updated_mono = now
            file.status = outcome.status.value
            if outcome.remote_file.size_bytes is not None:
                file.size_bytes = outcome.remote_file.size_bytes

    def mark_cancel_requested(self, run_id: int) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            run.cancel_requested = True
            return True

    def finish_run(self, run_id: int) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def snapshot(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            runs = [self._run_snapshot(run, now) for run in self._runs.values()]
        runs.sort(key=lambda item: item["run_id"])
        return {
            "active": bool(runs),
            "active_runs": len(runs),
            "runs": runs,
        }

    @staticmethod
    def _run_snapshot(run: _RunProgress, now: float) -> dict[str, Any]:
        files = [file.snapshot(now) for file in run.files.values()]
        files.sort(key=lambda item: item["run_file_id"])
        total_known = sum(
            item["size_bytes"] for item in files if item["size_bytes"] is not None
        )
        bytes_done = sum(item["bytes_done"] for item in files)
        average_bps = sum(item["average_bps"] for item in files)
        known_bytes_done = sum(
            item["bytes_done"]
            for item in files
            if item["size_bytes"] is not None
        )
        known_average_bps = sum(
            item["average_bps"]
            for item in files
            if item["size_bytes"] is not None
        )
        statuses: dict[str, int] = {}
        for item in files:
            statuses[item["status"]] = statuses.get(item["status"], 0) + 1
        return {
            "run_id": run.run_id,
            "connection_id": run.connection_id,
            "connection_name": run.connection_name,
            "trigger": run.trigger,
            "started_at": run.started_at,
            "cancel_requested": run.cancel_requested,
            "files_total": len(files),
            "files_completed": sum(
                statuses.get(status, 0)
                for status in ("ok", "skipped", "failed", "cancelled")
            ),
            "bytes_done": bytes_done,
            "size_bytes": total_known,
            "average_bps": round(average_bps, 2),
            "eta_s": _eta(total_known, known_bytes_done, known_average_bps),
            "percent": _percent(total_known, known_bytes_done),
            "statuses": statuses,
            "files": files,
        }


def _eta(size_bytes: int | None, bytes_done: int, speed: float) -> float | None:
    if size_bytes is None or speed <= 0 or bytes_done >= size_bytes:
        return None
    return round(max(0.0, size_bytes - bytes_done) / speed, 1)


def _percent(size_bytes: int | None, bytes_done: int) -> float | None:
    if size_bytes is None or size_bytes <= 0:
        return None
    return round(min(100.0, max(0.0, bytes_done * 100 / size_bytes)), 2)
