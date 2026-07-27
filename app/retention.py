"""Audit-data retention that never touches downloaded files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import Database


@dataclass(frozen=True)
class RetentionResult:
    runs_deleted: int
    files_deleted: int
    alerts_deleted: int
    logs_deleted: int
    exports_deleted: int


class RetentionService:
    def __init__(
        self,
        database: Database,
        *,
        run_logs: Path,
        exports: Path,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.database = database
        self.run_logs = run_logs
        self.exports = exports
        self.now = now

    def purge(self, *, days: int = 180) -> RetentionResult:
        if days < 1:
            raise ValueError("La retención debe ser de al menos un día.")
        cutoff = self.now().astimezone(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()
        with self.database.connect() as database:
            files = database.execute(
                """
                SELECT COUNT(*) AS count
                FROM run_files
                WHERE run_id IN (
                    SELECT id FROM runs WHERE started_at < ?
                )
                """,
                (cutoff_iso,),
            ).fetchone()["count"]
            alerts = database.execute(
                """
                SELECT COUNT(*) AS count
                FROM alerts_log
                WHERE run_id IN (
                    SELECT id FROM runs WHERE started_at < ?
                )
                """,
                (cutoff_iso,),
            ).fetchone()["count"]
            runs = database.execute(
                "DELETE FROM runs WHERE started_at < ?",
                (cutoff_iso,),
            ).rowcount
        logs = _delete_older_than(self.run_logs, "*.jsonl", cutoff)
        exports = _delete_older_than(self.exports, "*", cutoff)
        return RetentionResult(
            runs_deleted=int(runs),
            files_deleted=int(files),
            alerts_deleted=int(alerts),
            logs_deleted=logs,
            exports_deleted=exports,
        )


def _delete_older_than(
    directory: Path, pattern: str, cutoff: datetime
) -> int:
    if not directory.exists():
        return 0
    deleted = 0
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        if modified < cutoff:
            path.unlink()
            deleted += 1
    return deleted
