"""SQLite WAL database, sequential migrations, and connection CRUD."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
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
from app.logging_setup import redact_secrets
from app.errors import RecolectaError
from app.naming import collision_path, local_path_key

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
    2: (
        "ALTER TABLE run_files ADD COLUMN average_bps REAL",
    ),
    3: (
        "ALTER TABLE connections ADD COLUMN schedule_time TEXT",
    ),
    4: (
        """
        ALTER TABLE connections
        ADD COLUMN full_local_reconciliation INTEGER NOT NULL DEFAULT 0
        CHECK (full_local_reconciliation IN (0, 1))
        """,
        """
        UPDATE connections
        SET dest_template = '{remote_tree}'
        WHERE dest_template =
            '{client}\\{connection}\\{yyyy}\\{MM}\\{dd}\\{filename}'
        """,
        """
        ALTER TABLE runs
        ADD COLUMN scan_mode TEXT NOT NULL DEFAULT 'window'
        CHECK (scan_mode IN ('window', 'full_local_reconciliation'))
        """,
        """
        ALTER TABLE runs
        ADD COLUMN phase TEXT NOT NULL DEFAULT 'finished'
        """,
        """
        ALTER TABLE runs
        ADD COLUMN files_planned INTEGER NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE runs
        ADD COLUMN planned_bytes INTEGER NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE runs
        ADD COLUMN files_discovery_skipped INTEGER NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE run_files
        ADD COLUMN identity_key TEXT NOT NULL DEFAULT ''
        """,
        "ALTER TABLE run_files ADD COLUMN plan_status TEXT",
        """
        ALTER TABLE run_files
        ADD COLUMN reason TEXT NOT NULL DEFAULT ''
        """,
        "DROP INDEX IF EXISTS idx_file_identity",
        """
        CREATE INDEX idx_file_identity_lookup
        ON run_files(
            connection_id, remote_path, mtime_utc, size_bytes, status
        )
        """,
        """
        CREATE UNIQUE INDEX idx_run_file_identity
        ON run_files(run_id, identity_key)
        WHERE identity_key <> ''
        """,
        """
        CREATE INDEX idx_run_files_queue
        ON run_files(run_id, status, id)
        """,
        """
        CREATE TABLE destination_reservations (
            connection_id INTEGER NOT NULL
                REFERENCES connections(id) ON DELETE CASCADE,
            mapping_scope TEXT NOT NULL,
            remote_path TEXT NOT NULL,
            candidate_key BLOB NOT NULL,
            local_path TEXT NOT NULL,
            local_key BLOB NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(
                connection_id, mapping_scope, remote_path, candidate_key
            ),
            UNIQUE(local_key)
        )
        """,
    ),
    5: (
        """
        ALTER TABLE run_files
        ADD COLUMN timestamp_reliable INTEGER NOT NULL DEFAULT 0
        CHECK (timestamp_reliable IN (0, 1))
        """,
        """
        ALTER TABLE run_files
        ADD COLUMN timestamp_source TEXT NOT NULL DEFAULT ''
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
        scan_mode: str = "window",
    ) -> int:
        started = started_at or datetime.now(timezone.utc)
        if scan_mode not in {"window", "full_local_reconciliation"}:
            raise ValueError(f"Modo de exploración no soportado: {scan_mode}.")
        with self.database.connect() as database:
            cursor = database.execute(
                """
                INSERT INTO runs(
                    connection_id, trigger, window_start_utc, window_end_utc,
                    started_at, status, scan_mode, phase
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, 'discovering')
                """,
                (
                    connection_id,
                    trigger,
                    _aware_iso(window_start_utc),
                    _aware_iso(window_end_utc),
                    _aware_iso(started),
                    scan_mode,
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
        local_path: str | None = None,
        plan_status: str | None = None,
        reason: str = "",
    ) -> int:
        with self.database.connect() as database:
            cursor = database.execute(
                """
                INSERT INTO run_files(
                    run_id, connection_id, remote_path, size_bytes,
                    mtime_utc, timestamp_reliable, timestamp_source,
                    status, local_path, identity_key, plan_status, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    int(remote_file.timestamp_reliable),
                    remote_file.timestamp_source,
                    status,
                    local_path,
                    _identity_key(remote_file),
                    plan_status,
                    reason,
                ),
            )
            return int(cursor.lastrowid)

    def add_file_batch(
        self,
        *,
        run_id: int,
        connection_id: int,
        items: Sequence[
            tuple[
                "RemoteFile",
                str,
                str | None,
                str,
                str | None,
                str | None,
                str,
            ]
        ],
    ) -> list[int | None]:
        """Insert one discovery batch and return ids; duplicates return None."""
        inserted: list[int | None] = []
        with self.database.connect() as database:
            pending_candidates = tuple(
                dict.fromkeys(
                    remote_file.remote_path
                    for remote_file, status, *_ in items
                    if status == "pending"
                )
            )
            pending_paths: set[str] = set()
            for offset in range(0, len(pending_candidates), 500):
                path_batch = pending_candidates[offset : offset + 500]
                placeholders = ", ".join("?" for _ in path_batch)
                rows = database.execute(
                    f"""
                    SELECT remote_path
                    FROM run_files
                    WHERE run_id = ?
                      AND status IN ('pending', 'downloading')
                      AND remote_path IN ({placeholders})
                    """,
                    (run_id, *path_batch),
                ).fetchall()
                pending_paths.update(row["remote_path"] for row in rows)
            for (
                remote_file,
                status,
                plan_status,
                reason,
                local_path,
                error_type,
                error_msg,
            ) in items:
                if status == "pending" and remote_file.remote_path in pending_paths:
                    inserted.append(None)
                    continue
                cursor = database.execute(
                    """
                    INSERT OR IGNORE INTO run_files(
                        run_id, connection_id, remote_path, size_bytes,
                        mtime_utc, timestamp_reliable, timestamp_source,
                        status, local_path, identity_key, plan_status, reason,
                        error_type, error_msg, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        connection_id,
                        remote_file.remote_path,
                        remote_file.size_bytes,
                        (
                            remote_file.mtime_utc.isoformat(
                                timespec="seconds"
                            )
                            if remote_file.mtime_utc is not None
                            else None
                        ),
                        int(remote_file.timestamp_reliable),
                        remote_file.timestamp_source,
                        status,
                        local_path,
                        _identity_key(remote_file),
                        plan_status,
                        reason,
                        error_type,
                        redact_secrets(error_msg),
                        utc_now_iso() if status == "failed" else None,
                    ),
                )
                inserted.append(
                    int(cursor.lastrowid) if cursor.rowcount == 1 else None
                )
                if status == "pending" and cursor.rowcount == 1:
                    pending_paths.add(remote_file.remote_path)
        return inserted

    def successful_identities_for(
        self,
        connection_id: int,
        remote_files: Sequence["RemoteFile"],
    ) -> set[tuple[str, str | None, int | None]]:
        """Load successful identities only for one bounded discovery batch."""
        paths = tuple(dict.fromkeys(item.remote_path for item in remote_files))
        if not paths:
            return set()
        placeholders = ", ".join("?" for _ in paths)
        with self.database.connect() as database:
            rows = database.execute(
                f"""
                SELECT remote_path, mtime_utc, size_bytes
                FROM run_files
                WHERE connection_id = ? AND status = 'ok'
                  AND remote_path IN ({placeholders})
                """,
                (connection_id, *paths),
            ).fetchall()
        return {
            (
                row["remote_path"],
                _canonical_timestamp(row["mtime_utc"]),
                row["size_bytes"],
            )
            for row in rows
        }

    def reserve_destination(
        self,
        *,
        connection_id: int,
        mapping_scope: str,
        remote_path: str,
        candidate: Path,
    ) -> Path:
        """Reserve a stable collision-free local path across runs."""
        reserved = self.reserve_destinations(
            connection_id=connection_id,
            mapping_scope=mapping_scope,
            candidates=((remote_path, candidate),),
        )[0]
        if isinstance(reserved, RecolectaError):
            raise reserved
        return reserved

    def reserve_destinations(
        self,
        *,
        connection_id: int,
        mapping_scope: str,
        candidates: Sequence[tuple[str, Path]],
    ) -> list[Path | RecolectaError]:
        """Reserve one bounded mapping batch in a single transaction."""
        reserved: list[Path | RecolectaError] = []
        with self.database.connect() as database:
            for remote_path, candidate in candidates:
                candidate_key = local_path_key(candidate)
                existing = database.execute(
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
                    reserved.append(Path(existing["local_path"]))
                    continue

                selected = candidate
                suffix = hashlib.sha256(
                    remote_path.encode("utf-8", errors="surrogatepass")
                ).hexdigest()[:10]
                counter = 1
                while True:
                    try:
                        database.execute(
                            """
                            INSERT INTO destination_reservations(
                                connection_id, mapping_scope, remote_path,
                                candidate_key, local_path, local_key, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                connection_id,
                                mapping_scope,
                                remote_path,
                                candidate_key,
                                str(selected),
                                local_path_key(selected),
                                utc_now_iso(),
                            ),
                        )
                        reserved.append(selected)
                        break
                    except sqlite3.IntegrityError:
                        owner = database.execute(
                            """
                            SELECT connection_id, remote_path, local_path
                            FROM destination_reservations
                            WHERE local_key = ?
                            """,
                            (local_path_key(selected),),
                        ).fetchone()
                        if (
                            owner is not None
                            and int(owner["connection_id"]) == connection_id
                            and owner["remote_path"] == remote_path
                        ):
                            reserved.append(Path(owner["local_path"]))
                            break
                        if owner is None:
                            raise
                        marker = f"__{suffix}"
                        if counter > 1:
                            marker += f"_{counter}"
                        try:
                            selected = collision_path(candidate, marker)
                        except RecolectaError as exc:
                            reserved.append(exc)
                            break
                        counter += 1
        return reserved

    def update_discovery(
        self,
        run_id: int,
        *,
        files_found: int,
        files_planned: int,
        planned_bytes: int,
        files_skipped: int,
        phase: str = "downloading",
    ) -> None:
        with self.database.connect() as database:
            cursor = database.execute(
                """
                UPDATE runs
                SET files_found = ?, files_planned = ?, planned_bytes = ?,
                    files_discovery_skipped = ?, phase = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    max(0, files_found),
                    max(0, files_planned),
                    max(0, planned_bytes),
                    max(0, files_skipped),
                    phase,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"No existe la corrida activa {run_id}.")

    def claim_pending_batch(
        self, run_id: int, *, limit: int
    ) -> list[dict[str, Any]]:
        """Atomically claim a bounded queue batch for download workers."""
        bounded = max(1, min(int(limit), 5000))
        with self.database.connect() as database:
            rows = database.execute(
                """
                WITH selected AS (
                    SELECT id
                    FROM run_files
                    WHERE run_id = ? AND status = 'pending'
                    ORDER BY id
                    LIMIT ?
                )
                UPDATE run_files
                SET status = 'downloading',
                    started_at = COALESCE(started_at, ?)
                WHERE status = 'pending'
                  AND id IN (SELECT id FROM selected)
                RETURNING id, remote_path, size_bytes, mtime_utc,
                          timestamp_reliable, timestamp_source, local_path
                """,
                (run_id, bounded, utc_now_iso()),
            ).fetchall()
        return sorted(
            (dict(row) for row in rows),
            key=lambda row: int(row["id"]),
        )

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
        self.record_download_outcomes_batch(((run_file_id, outcome),))

    def record_download_outcomes_batch(
        self,
        items: Sequence[tuple[int, "DownloadOutcome"]],
    ) -> None:
        """Persist a completed worker batch with one SQLite commit."""
        if not items:
            return
        with self.database.connect() as database:
            for run_file_id, outcome in items:
                status_value = outcome.status.value
                cursor = database.execute(
                    """
                    UPDATE run_files
                    SET local_path = ?, size_bytes = ?, bytes_done = ?,
                        sha256 = ?, status = ?, attempts = ?,
                        error_type = ?, error_msg = ?, finished_at = ?,
                        duration_s = ?, average_bps = ?
                    WHERE id = ?
                    """,
                    (
                        (
                            str(outcome.local_path)
                            if outcome.local_path
                            else None
                        ),
                        outcome.remote_file.size_bytes,
                        outcome.bytes_done,
                        outcome.sha256,
                        status_value,
                        outcome.attempts,
                        (
                            outcome.error_type.value
                            if outcome.error_type
                            else None
                        ),
                        redact_secrets(outcome.error_msg),
                        utc_now_iso(),
                        outcome.duration_s,
                        (
                            outcome.bytes_done / outcome.duration_s
                            if outcome.duration_s > 0
                            else 0.0
                        ),
                        run_file_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise KeyError(
                        f"No existe el archivo de corrida {run_file_id}."
                    )

    def cancel_unfinished(self, run_id: int) -> int:
        """Mark queued work as cancelled when an operator stops a run."""
        with self.database.connect() as database:
            return database.execute(
                """
                UPDATE run_files
                SET status = 'cancelled', error_type = 'interrupted',
                    error_msg = 'La corrida fue cancelada por el usuario.',
                    finished_at = ?
                WHERE run_id = ? AND status IN ('pending', 'downloading')
                """,
                (utc_now_iso(), run_id),
            ).rowcount

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
                    COUNT(*) AS persisted_files,
                    SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)
                        AS files_downloaded,
                    SUM(
                        CASE
                            WHEN status IN ('skipped', 'duplicate')
                             AND (
                                plan_status IS NULL
                                OR plan_status IN (
                                    'planned',
                                    'local_missing',
                                    'local_different'
                                )
                             )
                            THEN 1 ELSE 0
                        END
                    ) AS runtime_skipped,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
                        AS files_failed,
                    SUM(CASE WHEN status = 'ok' THEN bytes_done ELSE 0 END)
                        AS bytes_downloaded
                FROM run_files
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            discovery = database.execute(
                """
                SELECT files_found, files_discovery_skipped
                FROM runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if discovery is None:
                raise KeyError(f"No existe la corrida {run_id}.")
            files_found = max(
                int(discovery["files_found"] or 0),
                int(counts["persisted_files"] or 0),
            )
            files_skipped = (
                int(discovery["files_discovery_skipped"] or 0)
                + int(counts["runtime_skipped"] or 0)
            )
            cursor = database.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, files_found = ?,
                    files_downloaded = ?, files_skipped = ?, files_failed = ?,
                    bytes_downloaded = ?, error_type = ?, error_msg = ?,
                    phase = 'finished'
                WHERE id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    files_found,
                    counts["files_downloaded"] or 0,
                    files_skipped,
                    counts["files_failed"] or 0,
                    counts["bytes_downloaded"] or 0,
                    error_type,
                    redact_secrets(error_msg),
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
        """Terminalize orphaned runs; a new run can reuse deterministic parts."""
        now = utc_now_iso()
        with self.database.connect() as database:
            files = database.execute(
                """
                UPDATE run_files
                SET status = 'failed', error_type = 'interrupted',
                    error_msg = 'La aplicación se reinició durante la descarga.',
                    finished_at = ?
                WHERE status IN ('pending', 'downloading')
                  AND run_id IN (SELECT id FROM runs WHERE status = 'running')
                """,
                (now,),
            ).rowcount
            runs = database.execute(
                """
                UPDATE runs
                SET status = 'failed', finished_at = ?,
                    error_type = 'interrupted',
                    error_msg = 'La aplicación se reinició durante la corrida.',
                    phase = 'finished',
                    files_found = MAX(
                        files_found,
                        (
                            SELECT COUNT(*)
                            FROM run_files f
                            WHERE f.run_id = runs.id
                        )
                    ),
                    files_downloaded = (
                        SELECT COUNT(*)
                        FROM run_files f
                        WHERE f.run_id = runs.id AND f.status = 'ok'
                    ),
                    files_skipped = files_discovery_skipped + (
                        SELECT COUNT(*)
                        FROM run_files f
                        WHERE f.run_id = runs.id
                          AND f.status IN ('skipped', 'duplicate')
                          AND (
                            f.plan_status IS NULL
                            OR f.plan_status IN (
                                'planned',
                                'local_missing',
                                'local_different'
                            )
                          )
                    ),
                    files_failed = (
                        SELECT COUNT(*)
                        FROM run_files f
                        WHERE f.run_id = runs.id AND f.status = 'failed'
                    ),
                    bytes_downloaded = (
                        SELECT COALESCE(SUM(f.bytes_done), 0)
                        FROM run_files f
                        WHERE f.run_id = runs.id AND f.status = 'ok'
                    )
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
                (
                    error_type,
                    redact_secrets(error_msg),
                    utc_now_iso(),
                    run_id,
                ),
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
        if status == "no_files":
            clauses.append("r.status = 'ok' AND r.files_found = 0")
        elif status == "no_changes":
            clauses.append(
                "r.status = 'ok' AND r.files_found > 0 "
                "AND r.files_downloaded = 0"
            )
        elif status == "completed":
            clauses.append(
                "r.status = 'ok' AND r.files_downloaded > 0"
            )
        elif status:
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
                       r.files_found AS last_files_found,
                       r.files_downloaded AS last_files_downloaded,
                       r.files_skipped AS last_files_skipped,
                       r.files_failed AS last_files_failed,
                       r.bytes_downloaded AS last_bytes_downloaded,
                       r.error_type AS last_error_type
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
        "schedule_time": connection.schedule_time,
        "dest_root": connection.dest_root,
        "dest_template": connection.dest_template,
        "full_local_reconciliation": int(
            connection.full_local_reconciliation
        ),
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
        schedule_time=row["schedule_time"],
        dest_root=row["dest_root"],
        dest_template=row["dest_template"],
        full_local_reconciliation=bool(
            row["full_local_reconciliation"]
        ),
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


def _identity_key(remote_file: "RemoteFile") -> str:
    timestamp = (
        remote_file.mtime_utc.isoformat(timespec="seconds")
        if remote_file.mtime_utc is not None
        else ""
    )
    material = "\x1f".join(
        (
            remote_file.remote_path,
            timestamp,
            "" if remote_file.size_bytes is None else str(remote_file.size_bytes),
        )
    )
    return hashlib.sha256(
        material.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _canonical_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
