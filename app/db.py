"""SQLite WAL database, sequential migrations, and connection CRUD."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final

from app.models import (
    AuthType,
    ConflictMode,
    Connection,
    PostAction,
    Protocol,
    VerifyMode,
    WindowMode,
    utc_now_iso,
)

if TYPE_CHECKING:
    from app.downloader import DownloadOutcome
    from app.platform.secretstore import SecretStore
    from app.transports.base import RemoteFile


MIGRATIONS: Final[dict[int, tuple[str, ...]]] = {
    1: (
        """
        CREATE TABLE connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            client TEXT NOT NULL DEFAULT '',
            protocol TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            secret_encrypted TEXT,
            auth_type TEXT NOT NULL DEFAULT 'password',
            key_path TEXT,
            ssl_mode TEXT NOT NULL DEFAULT 'preferred',
            remote_paths_json TEXT NOT NULL DEFAULT '[]',
            recursive INTEGER NOT NULL DEFAULT 0,
            max_depth INTEGER NOT NULL DEFAULT 3,
            include_globs_json TEXT NOT NULL DEFAULT '[]',
            exclude_globs_json TEXT NOT NULL DEFAULT '[]',
            min_size_bytes INTEGER,
            max_size_bytes INTEGER,
            window_mode TEXT NOT NULL DEFAULT 'calendar_day',
            window_hours INTEGER NOT NULL DEFAULT 24,
            window_overlap_min INTEGER NOT NULL DEFAULT 15,
            quiet_period_s INTEGER NOT NULL DEFAULT 120,
            timezone TEXT NOT NULL DEFAULT 'America/Bogota',
            dest_root TEXT NOT NULL,
            dest_template TEXT NOT NULL DEFAULT
                '{client}\\{connection}\\{yyyy}\\{MM}\\{dd}\\{filename}',
            on_conflict TEXT NOT NULL DEFAULT 'skip',
            verify_mode TEXT NOT NULL DEFAULT 'size',
            max_parallel_files INTEGER NOT NULL DEFAULT 2,
            bandwidth_limit_kbps INTEGER,
            timeout_s REAL NOT NULL DEFAULT 30,
            retries INTEGER NOT NULL DEFAULT 3,
            post_action TEXT NOT NULL DEFAULT 'none',
            post_action_path TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL
                REFERENCES connections(id) ON DELETE CASCADE,
            trigger TEXT NOT NULL,
            window_start_utc TEXT NOT NULL,
            window_end_utc TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            files_found INTEGER DEFAULT 0,
            files_downloaded INTEGER DEFAULT 0,
            files_skipped INTEGER DEFAULT 0,
            files_failed INTEGER DEFAULT 0,
            bytes_downloaded INTEGER DEFAULT 0,
            error_type TEXT,
            error_msg TEXT NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX idx_runs_conn_started ON runs(connection_id, started_at)",
        """
        CREATE TABLE run_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            connection_id INTEGER NOT NULL,
            remote_path TEXT NOT NULL,
            local_path TEXT,
            size_bytes INTEGER,
            bytes_done INTEGER NOT NULL DEFAULT 0,
            mtime_utc TEXT,
            sha256 TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_type TEXT,
            error_msg TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            finished_at TEXT,
            duration_s REAL
        )
        """,
        "CREATE INDEX idx_run_files_run ON run_files(run_id, status)",
        """
        CREATE UNIQUE INDEX idx_file_identity
            ON run_files(connection_id, remote_path, mtime_utc, size_bytes)
            WHERE status = 'ok'
        """,
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
            cause TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE UNIQUE INDEX idx_alert_once
            ON alerts_log(run_id, cause, channel)
            WHERE run_id IS NOT NULL
        """,
    ),
}


class Database:
    """Own database connections and schema versioning."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Apply every pending migration exactly once and in order."""
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            }
            for version, statements in sorted(MIGRATIONS.items()):
                if version in applied:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now_iso()),
                )

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])


_UNSET: Final = object()


class ConnectionRepository:
    """Persist connections while keeping encrypted secrets behind the repository."""

    def __init__(self, database: Database, secret_store: "SecretStore") -> None:
        self.database = database
        self.secret_store = secret_store

    def create(self, connection: Connection, *, secret: str | None = None) -> Connection:
        normalized = connection.normalized()
        now = utc_now_iso()
        token = self.secret_store.encrypt(secret) if secret else None
        values = _connection_db_values(
            normalized,
            secret_encrypted=token,
            created_at=now,
            updated_at=now,
        )
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self.database.connect() as database:
            cursor = database.execute(
                f"INSERT INTO connections ({columns}) VALUES ({placeholders})",
                values,
            )
            connection_id = int(cursor.lastrowid)
        return self.get(connection_id)

    def get(self, connection_id: int) -> Connection:
        with self.database.connect() as database:
            row = database.execute(
                "SELECT * FROM connections WHERE id = ?", (connection_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe la conexión {connection_id}.")
        return _connection_from_row(row)

    def list(self, *, enabled_only: bool = False) -> list[Connection]:
        query = "SELECT * FROM connections"
        parameters: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled = ?"
            parameters = (1,)
        query += " ORDER BY name COLLATE NOCASE, id"
        with self.database.connect() as database:
            rows = database.execute(query, parameters).fetchall()
        return [_connection_from_row(row) for row in rows]

    def update(
        self,
        connection_id: int,
        changes: dict[str, Any],
        *,
        secret: str | None | object = _UNSET,
    ) -> Connection:
        current = self.get(connection_id)
        updated = current.with_changes(changes)
        with self.database.connect() as database:
            row = database.execute(
                "SELECT secret_encrypted FROM connections WHERE id = ?",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"No existe la conexión {connection_id}.")
            token = row["secret_encrypted"]
            if secret is not _UNSET:
                token = self.secret_store.encrypt(secret) if secret else None
            values = _connection_db_values(
                updated,
                secret_encrypted=token,
                created_at=current.created_at or utc_now_iso(),
                updated_at=utc_now_iso(),
            )
            assignments = ", ".join(f"{name} = :{name}" for name in values)
            values["id"] = connection_id
            database.execute(
                f"UPDATE connections SET {assignments} WHERE id = :id",
                values,
            )
        return self.get(connection_id)

    def delete(self, connection_id: int) -> bool:
        with self.database.connect() as database:
            cursor = database.execute(
                "DELETE FROM connections WHERE id = ?", (connection_id,)
            )
            return cursor.rowcount > 0

    def get_secret(self, connection_id: int) -> str | None:
        with self.database.connect() as database:
            row = database.execute(
                "SELECT secret_encrypted FROM connections WHERE id = ?",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe la conexión {connection_id}.")
        token = row["secret_encrypted"]
        return self.secret_store.decrypt(token) if token else None


class RunRepository:
    """Persist run/file lifecycle and download outcomes."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def start_run(
        self,
        *,
        connection_id: int,
        trigger: str,
        window_start_utc: datetime,
        window_end_utc: datetime,
        started_at: datetime | None = None,
    ) -> int:
        started = started_at or datetime.now(timezone.utc)
        with self.database.connect() as database:
            cursor = database.execute(
                """
                INSERT INTO runs(
                    connection_id, trigger, window_start_utc, window_end_utc,
                    started_at, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
                """,
                (
                    connection_id,
                    trigger,
                    _aware_iso(window_start_utc),
                    _aware_iso(window_end_utc),
                    _aware_iso(started),
                ),
            )
            return int(cursor.lastrowid)

    def add_file(
        self,
        *,
        run_id: int,
        connection_id: int,
        remote_file: "RemoteFile",
        status: str = "pending",
    ) -> int:
        with self.database.connect() as database:
            cursor = database.execute(
                """
                INSERT INTO run_files(
                    run_id, connection_id, remote_path, size_bytes,
                    mtime_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    connection_id,
                    remote_file.remote_path,
                    remote_file.size_bytes,
                    (
                        remote_file.mtime_utc.isoformat(timespec="seconds")
                        if remote_file.mtime_utc is not None
                        else None
                    ),
                    status,
                ),
            )
            return int(cursor.lastrowid)

    def mark_downloading(
        self, run_file_id: int, *, attempts: int, bytes_done: int
    ) -> None:
        with self.database.connect() as database:
            database.execute(
                """
                UPDATE run_files
                SET status = 'downloading', attempts = ?, bytes_done = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE id = ?
                """,
                (attempts, bytes_done, utc_now_iso(), run_file_id),
            )

    def update_file_progress(self, run_file_id: int, bytes_done: int) -> None:
        """Persist a sampled byte count without changing terminal state."""
        with self.database.connect() as database:
            cursor = database.execute(
                """
                UPDATE run_files
                SET bytes_done = MAX(bytes_done, ?)
                WHERE id = ? AND status = 'downloading'
                """,
                (max(0, bytes_done), run_file_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No existe el archivo activo {run_file_id}.")

    def record_download_outcome(
        self, run_file_id: int, outcome: "DownloadOutcome"
    ) -> None:
        status_value = outcome.status.value
        persisted_status = "pending" if status_value == "cancelled" else status_value
        finished_at = None if status_value == "cancelled" else utc_now_iso()
        with self.database.connect() as database:
            cursor = database.execute(
                """
                UPDATE run_files
                SET local_path = ?, size_bytes = ?, bytes_done = ?, sha256 = ?,
                    status = ?, attempts = ?, error_type = ?, error_msg = ?,
                    finished_at = ?, duration_s = ?
                WHERE id = ?
                """,
                (
                    str(outcome.local_path) if outcome.local_path else None,
                    outcome.remote_file.size_bytes,
                    outcome.bytes_done,
                    outcome.sha256,
                    persisted_status,
                    outcome.attempts,
                    outcome.error_type.value if outcome.error_type else None,
                    outcome.error_msg,
                    finished_at,
                    outcome.duration_s,
                    run_file_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No existe el archivo de corrida {run_file_id}.")

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        error_type: str | None = None,
        error_msg: str = "",
    ) -> None:
        with self.database.connect() as database:
            counts = database.execute(
                """
                SELECT
                    COUNT(*) AS files_found,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)
                        AS files_downloaded,
                    SUM(CASE WHEN status IN ('skipped', 'duplicate') THEN 1 ELSE 0 END)
                        AS files_skipped,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                        AS files_failed,
                    SUM(CASE WHEN status = 'ok' THEN bytes_done ELSE 0 END)
                        AS bytes_downloaded
                FROM run_files
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            cursor = database.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, files_found = ?,
                    files_downloaded = ?, files_skipped = ?, files_failed = ?,
                    bytes_downloaded = ?, error_type = ?, error_msg = ?
                WHERE id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    counts["files_found"] or 0,
                    counts["files_downloaded"] or 0,
                    counts["files_skipped"] or 0,
                    counts["files_failed"] or 0,
                    counts["bytes_downloaded"] or 0,
                    error_type,
                    error_msg,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No existe la corrida {run_id}.")

    def last_successful_end(self, connection_id: int) -> datetime | None:
        with self.database.connect() as database:
            row = database.execute(
                """
                SELECT window_end_utc
                FROM runs
                WHERE connection_id = ? AND status = 'ok'
                ORDER BY window_end_utc DESC
                LIMIT 1
                """,
                (connection_id,),
            ).fetchone()
        return _parse_datetime(row["window_end_utc"]) if row else None

    def has_successful_window(
        self,
        connection_id: int,
        *,
        window_start_utc: datetime,
        window_end_utc: datetime,
    ) -> bool:
        expected_start = window_start_utc.astimezone(timezone.utc)
        expected_end = window_end_utc.astimezone(timezone.utc)
        with self.database.connect() as database:
            rows = database.execute(
                """
                SELECT window_start_utc, window_end_utc
                FROM runs
                WHERE connection_id = ? AND status = 'ok'
                """,
                (connection_id,),
            ).fetchall()
        return any(
            _parse_datetime(row["window_start_utc"]) == expected_start
            and _parse_datetime(row["window_end_utc"]) == expected_end
            for row in rows
        )

    def recover_interrupted(self) -> tuple[int, int]:
        """Fail orphaned running runs and return downloading files to pending."""
        now = utc_now_iso()
        with self.database.connect() as database:
            files = database.execute(
                """
                UPDATE run_files
                SET status = 'pending', error_type = 'interrupted',
                    error_msg = 'La aplicación se reinició durante la descarga.'
                WHERE status = 'downloading'
                  AND run_id IN (SELECT id FROM runs WHERE status = 'running')
                """
            ).rowcount
            runs = database.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?,
                    error_type = 'interrupted',
                    error_msg = 'La aplicación se reinició durante la corrida.'
                WHERE status = 'running'
                """,
                (now,),
            ).rowcount
        return runs, files

    def fail_unfinished(
        self, run_id: int, *, error_type: str, error_msg: str
    ) -> int:
        with self.database.connect() as database:
            return database.execute(
                """
                UPDATE run_files
                SET status = 'failed', error_type = ?, error_msg = ?,
                    finished_at = ?
                WHERE run_id = ? AND status IN ('pending', 'downloading')
                """,
                (error_type, error_msg, utc_now_iso(), run_id),
            ).rowcount

    def list_runs(
        self,
        *,
        connection_id: int | None = None,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if connection_id is not None:
            clauses.append("r.connection_id = ?")
            parameters.append(connection_id)
        if status:
            clauses.append("r.status = ?")
            parameters.append(status)
        if date_from:
            clauses.append("date(r.started_at) >= date(?)")
            parameters.append(date_from)
        if date_to:
            clauses.append("date(r.started_at) <= date(?)")
            parameters.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((max(1, min(limit, 500)), max(0, offset)))
        with self.database.connect() as database:
            rows = database.execute(
                f"""
                SELECT r.*, c.name AS connection_name
                FROM runs r
                JOIN connections c ON c.id = r.connection_id
                {where}
                ORDER BY r.started_at DESC, r.id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any]:
        with self.database.connect() as database:
            row = database.execute(
                """
                SELECT r.*, c.name AS connection_name
                FROM runs r
                JOIN connections c ON c.id = r.connection_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"No existe la corrida {run_id}.")
        return dict(row)

    def list_files(
        self,
        *,
        run_id: int | None = None,
        connection_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id is not None:
            clauses.append("f.run_id = ?")
            parameters.append(run_id)
        if connection_id is not None:
            clauses.append("f.connection_id = ?")
            parameters.append(connection_id)
        if status:
            clauses.append("f.status = ?")
            parameters.append(status)
        if search:
            clauses.append(
                "(f.remote_path LIKE ? ESCAPE '\\' OR f.local_path LIKE ? ESCAPE '\\')"
            )
            escaped = (
                search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        if date_from:
            clauses.append("date(r.started_at) >= date(?)")
            parameters.append(date_from)
        if date_to:
            clauses.append("date(r.started_at) <= date(?)")
            parameters.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((max(1, min(limit, 1000)), max(0, offset)))
        with self.database.connect() as database:
            rows = database.execute(
                f"""
                SELECT f.*, r.started_at AS run_started_at,
                       c.name AS connection_name
                FROM run_files f
                JOIN runs r ON r.id = f.run_id
                JOIN connections c ON c.id = f.connection_id
                {where}
                ORDER BY r.started_at DESC, f.id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def dashboard_summary(self) -> list[dict[str, Any]]:
        with self.database.connect() as database:
            rows = database.execute(
                """
                SELECT c.id, c.name, c.client, c.protocol, c.enabled,
                       r.id AS last_run_id, r.started_at AS last_started_at,
                       r.status AS last_status,
                       r.files_downloaded AS last_files_downloaded,
                       r.files_failed AS last_files_failed,
                       r.bytes_downloaded AS last_bytes_downloaded
                FROM connections c
                LEFT JOIN runs r ON r.id = (
                    SELECT r2.id
                    FROM runs r2
                    WHERE r2.connection_id = c.id
                    ORDER BY r2.started_at DESC, r2.id DESC
                    LIMIT 1
                )
                ORDER BY c.name COLLATE NOCASE, c.id
                """
            ).fetchall()
        return [dict(row) for row in rows]


def _connection_db_values(
    connection: Connection,
    *,
    secret_encrypted: str | None,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    return {
        "name": connection.name,
        "client": connection.client,
        "protocol": connection.protocol.value,
        "host": connection.host,
        "port": connection.port,
        "username": connection.username,
        "secret_encrypted": secret_encrypted,
        "auth_type": connection.auth_type.value,
        "key_path": connection.key_path,
        "ssl_mode": connection.ssl_mode,
        "remote_paths_json": json.dumps(connection.remote_paths, ensure_ascii=False),
        "recursive": int(connection.recursive),
        "max_depth": connection.max_depth,
        "include_globs_json": json.dumps(connection.include_globs, ensure_ascii=False),
        "exclude_globs_json": json.dumps(connection.exclude_globs, ensure_ascii=False),
        "min_size_bytes": connection.min_size_bytes,
        "max_size_bytes": connection.max_size_bytes,
        "window_mode": connection.window_mode.value,
        "window_hours": connection.window_hours,
        "window_overlap_min": connection.window_overlap_min,
        "quiet_period_s": connection.quiet_period_s,
        "timezone": connection.timezone,
        "dest_root": connection.dest_root,
        "dest_template": connection.dest_template,
        "on_conflict": connection.on_conflict.value,
        "verify_mode": connection.verify_mode.value,
        "max_parallel_files": connection.max_parallel_files,
        "bandwidth_limit_kbps": connection.bandwidth_limit_kbps,
        "timeout_s": connection.timeout_s,
        "retries": connection.retries,
        "post_action": connection.post_action.value,
        "post_action_path": connection.post_action_path,
        "enabled": int(connection.enabled),
        "notes": connection.notes,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _connection_from_row(row: sqlite3.Row) -> Connection:
    return Connection(
        id=int(row["id"]),
        name=row["name"],
        client=row["client"],
        protocol=Protocol(row["protocol"]),
        host=row["host"],
        port=int(row["port"]),
        username=row["username"],
        auth_type=AuthType(row["auth_type"]),
        key_path=row["key_path"],
        ssl_mode=row["ssl_mode"],
        remote_paths=tuple(json.loads(row["remote_paths_json"])),
        recursive=bool(row["recursive"]),
        max_depth=int(row["max_depth"]),
        include_globs=tuple(json.loads(row["include_globs_json"])),
        exclude_globs=tuple(json.loads(row["exclude_globs_json"])),
        min_size_bytes=row["min_size_bytes"],
        max_size_bytes=row["max_size_bytes"],
        window_mode=WindowMode(row["window_mode"]),
        window_hours=int(row["window_hours"]),
        window_overlap_min=int(row["window_overlap_min"]),
        quiet_period_s=int(row["quiet_period_s"]),
        timezone=row["timezone"],
        dest_root=row["dest_root"],
        dest_template=row["dest_template"],
        on_conflict=ConflictMode(row["on_conflict"]),
        verify_mode=VerifyMode(row["verify_mode"]),
        max_parallel_files=int(row["max_parallel_files"]),
        bandwidth_limit_kbps=row["bandwidth_limit_kbps"],
        timeout_s=float(row["timeout_s"]),
        retries=int(row["retries"]),
        post_action=PostAction(row["post_action"]),
        post_action_path=row["post_action_path"],
        enabled=bool(row["enabled"]),
        notes=row["notes"],
        has_secret=bool(row["secret_encrypted"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _aware_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Los timestamps de corrida deben incluir zona horaria.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp persistido sin zona horaria.")
    return parsed.astimezone(timezone.utc)
