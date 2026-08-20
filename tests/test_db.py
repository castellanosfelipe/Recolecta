import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import MIGRATIONS, ConnectionRepository, Database, RunRepository
from app.models import Connection, Protocol
from app.platform.secrets_fernet import FernetSecretStore
from app.transports.base import RemoteFile


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "data" / "recolecta.db")
    value.initialize()
    return value


@pytest.fixture
def repository(database: Database) -> ConnectionRepository:
    return ConnectionRepository(database, FernetSecretStore(Fernet.generate_key()))


def connection(name: str = "SFTP Producción") -> Connection:
    return Connection(
        name=name,
        client="Cliente A",
        protocol=Protocol.SFTP,
        host="10.0.0.10",
        username="monitor",
        remote_paths=("/entrada", "/salida"),
        include_globs=("*.csv",),
        exclude_globs=("tmp_*",),
        dest_root=r"D:\Descargas",
        notes="Carga nocturna",
    )


def test_database_uses_wal_foreign_keys_and_sequential_migrations(
    database: Database,
) -> None:
    database.initialize()
    assert database.schema_version() == max(MIGRATIONS)
    with database.connect() as db:
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_keys").fetchone()[0]
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert {
        "connections",
        "runs",
        "run_files",
        "settings",
        "alerts_log",
        "schema_migrations",
    } <= tables


def test_migration_from_release_schema_preserves_connections(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "legacy-v3.db")
    with database.connect() as legacy:
        legacy.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        for version in (1, 2, 3):
            for statement in MIGRATIONS[version]:
                legacy.execute(statement)
            legacy.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, '2026-07-29T00:00:00+00:00')
                """,
                (version,),
            )
        legacy.execute(
            """
            INSERT INTO connections(
                name, protocol, host, port, ssl_mode, dest_root, dest_template,
                created_at, updated_at
            ) VALUES (
                'Legada', 'SFTP', 'legacy.test', 22, 'preferred', 'downloads',
                ?,
                '2026-07-29T00:00:00+00:00',
                '2026-07-29T00:00:00+00:00'
            )
            """,
            (r"{client}\{connection}\{yyyy}\{MM}\{dd}\{filename}",),
        )
        legacy.execute(
            """
            INSERT INTO runs(
                connection_id, trigger, window_start_utc, window_end_utc,
                started_at, finished_at, status, files_found
            ) VALUES (
                1, 'manual',
                '2026-07-28T00:00:00+00:00',
                '2026-07-29T00:00:00+00:00',
                '2026-07-29T02:00:00+00:00',
                '2026-07-29T02:01:00+00:00',
                'ok', 0
            )
            """
        )

    database.initialize()

    with database.connect() as upgraded:
        connection = upgraded.execute(
            "SELECT * FROM connections WHERE name = 'Legada'"
        ).fetchone()
        reservation_columns = {
            row["name"]: row["type"]
            for row in upgraded.execute(
                "PRAGMA table_info(destination_reservations)"
            )
        }
        run_file_columns = {
            row["name"]: row["type"]
            for row in upgraded.execute("PRAGMA table_info(run_files)")
        }
        run_columns = {
            row["name"]: row
            for row in upgraded.execute("PRAGMA table_info(runs)")
        }
        legacy_run = upgraded.execute(
            "SELECT * FROM runs WHERE id = 1"
        ).fetchone()
    assert database.schema_version() == max(MIGRATIONS)
    assert connection["full_local_reconciliation"] == 0
    assert connection["dest_template"] == "{remote_tree}"
    assert connection["ssl_mode"] == "required"
    assert reservation_columns["candidate_key"] == "BLOB"
    assert reservation_columns["local_key"] == "BLOB"
    assert run_file_columns["timestamp_reliable"] == "INTEGER"
    assert run_file_columns["timestamp_source"] == "TEXT"
    assert run_columns["discovery_paths_json"]["notnull"] == 0
    assert run_columns["discovery_recursive"]["notnull"] == 0
    assert run_columns["discovery_max_depth"]["notnull"] == 0
    assert run_columns["warnings_json"]["notnull"] == 1
    assert run_columns["notices_json"]["notnull"] == 1
    assert legacy_run["discovery_paths_json"] is None
    assert legacy_run["discovery_recursive"] is None
    assert legacy_run["discovery_max_depth"] is None
    assert legacy_run["warnings_json"] == "[]"
    assert legacy_run["notices_json"] == "[]"


def test_create_read_list_update_and_delete_connection(
    repository: ConnectionRepository,
) -> None:
    created = repository.create(connection(), secret="clave-super-secreta")
    assert created.id is not None
    assert created.has_secret is True
    assert created.port == 22
    assert created.remote_paths == ("/entrada", "/salida")
    assert repository.get_secret(created.id) == "clave-super-secreta"

    listed = repository.list()
    assert [item.id for item in listed] == [created.id]

    updated = repository.update(
        created.id,
        {
            "name": "SFTP Renombrado",
            "protocol": "FTP",
            "port": None,
            "schedule_time": "18:40",
            "enabled": False,
        },
    )
    assert updated.name == "SFTP Renombrado"
    assert updated.protocol == Protocol.FTP
    assert updated.port == 21
    assert updated.schedule_time == "18:40"
    assert repository.get_secret(created.id) == "clave-super-secreta"
    assert repository.list(enabled_only=True) == []

    replaced = repository.update(created.id, {}, secret="nueva-clave")
    assert replaced.has_secret
    assert repository.get_secret(created.id) == "nueva-clave"

    cleared = repository.update(created.id, {}, secret=None)
    assert not cleared.has_secret
    assert repository.get_secret(created.id) is None

    assert repository.delete(created.id)
    assert not repository.delete(created.id)
    with pytest.raises(KeyError, match=str(created.id)):
        repository.get(created.id)


def test_plaintext_secret_is_never_stored_in_database(
    database: Database, repository: ConnectionRepository
) -> None:
    created = repository.create(connection(), secret="no-guardar-en-claro")
    with database.connect() as db:
        row = db.execute(
            "SELECT secret_encrypted FROM connections WHERE id = ?", (created.id,)
        ).fetchone()
    token = row["secret_encrypted"]
    assert token.startswith("fernet:")
    assert "no-guardar-en-claro" not in token
    assert "secret_encrypted" not in created.to_public_dict()


def test_foreign_keys_cascade_runs_and_files(
    database: Database, repository: ConnectionRepository
) -> None:
    created = repository.create(connection())
    with database.connect() as db:
        run_id = db.execute(
            """
            INSERT INTO runs(
                connection_id, trigger, window_start_utc, window_end_utc,
                started_at, status
            ) VALUES (?, 'manual', '2026-01-01T00:00:00+00:00',
                      '2026-01-02T00:00:00+00:00',
                      '2026-01-02T02:00:00+00:00', 'running')
            """,
            (created.id,),
        ).lastrowid
        db.execute(
            """
            INSERT INTO run_files(
                run_id, connection_id, remote_path, status
            ) VALUES (?, ?, '/entrada/a.csv', 'pending')
            """,
            (run_id, created.id),
        )
    repository.delete(created.id)
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM run_files").fetchone()[0] == 0


def test_file_identity_is_unique_per_run_but_can_be_repaired_later(
    database: Database, repository: ConnectionRepository
) -> None:
    created = repository.create(connection())
    runs = RunRepository(database)
    window_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    window_end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    source = RemoteFile(
        "/entrada/a.csv",
        100,
        datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    first_run = runs.start_run(
        connection_id=created.id,
        trigger="manual",
        window_start_utc=window_start,
        window_end_utc=window_end,
    )
    runs.add_file(
        run_id=first_run,
        connection_id=created.id,
        remote_file=source,
        status="ok",
    )
    with pytest.raises(sqlite3.IntegrityError):
        runs.add_file(
            run_id=first_run,
            connection_id=created.id,
            remote_file=source,
            status="ok",
        )

    repair_run = runs.start_run(
        connection_id=created.id,
        trigger="manual",
        window_start_utc=window_start,
        window_end_utc=window_end,
        scan_mode="full_local_reconciliation",
    )
    repair_file = runs.add_file(
        run_id=repair_run,
        connection_id=created.id,
        remote_file=source,
        status="ok",
    )
    assert repair_file > 0


def test_run_scope_snapshot_is_optional_and_does_not_follow_connection_edits(
    database: Database,
    repository: ConnectionRepository,
) -> None:
    created = repository.create(connection())
    runs = RunRepository(database)
    started = datetime(2026, 1, 2, tzinfo=timezone.utc)
    legacy_id = runs.start_run(
        connection_id=created.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
    )
    snapshot_id = runs.start_run(
        connection_id=created.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
        remote_paths=("/entrada", "/archivo histórico"),
        recursive=True,
        max_depth=4,
    )

    repository.update(
        created.id,
        {
            "remote_paths": ("/editada",),
            "recursive": False,
            "max_depth": 0,
        },
    )
    legacy = runs.get_run(legacy_id)
    snapshot = runs.get_run(snapshot_id)

    assert legacy["discovery_paths_json"] is None
    assert legacy["discovery_recursive"] is None
    assert legacy["discovery_max_depth"] is None
    assert json.loads(snapshot["discovery_paths_json"]) == [
        "/entrada",
        "/archivo histórico",
    ]
    assert snapshot["discovery_recursive"] == 1
    assert snapshot["discovery_max_depth"] == 4


def test_warning_only_legacy_partials_close_windows_and_follow_result_filters(
    database: Database,
    repository: ConnectionRepository,
) -> None:
    created = repository.create(connection())
    runs = RunRepository(database)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def insert_partial(
        day: int,
        *,
        files_found: int,
        files_downloaded: int,
        files_failed: int = 0,
        error_type: str | None = None,
        error_msg: str = "",
        warnings_json: str = "[]",
    ) -> int:
        window_start = base + timedelta(days=day)
        window_end = window_start + timedelta(days=1)
        with database.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO runs(
                    connection_id, trigger, window_start_utc,
                    window_end_utc, started_at, finished_at, status,
                    files_found, files_downloaded, files_failed, error_type,
                    error_msg, warnings_json
                ) VALUES (
                    ?, 'schedule', ?, ?, ?, ?, 'partial', ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    created.id,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    (window_end + timedelta(hours=1)).isoformat(),
                    (window_end + timedelta(hours=2)).isoformat(),
                    files_found,
                    files_downloaded,
                    files_failed,
                    error_type,
                    error_msg,
                    warnings_json,
                ),
            )
        return int(cursor.lastrowid)

    empty_id = insert_partial(0, files_found=0, files_downloaded=0)
    unchanged_id = insert_partial(1, files_found=5, files_downloaded=0)
    completed_id = insert_partial(2, files_found=5, files_downloaded=4)
    failed_file_id = insert_partial(
        3,
        files_found=5,
        files_downloaded=4,
        files_failed=1,
    )
    errored_id = insert_partial(
        4,
        files_found=0,
        files_downloaded=0,
        error_type="protocol",
    )
    messaged_id = insert_partial(
        5,
        files_found=0,
        files_downloaded=0,
        error_msg="Fallo heredado sin categorizar.",
    )
    warned_id = insert_partial(
        6,
        files_found=0,
        files_downloaded=0,
        warnings_json='["Ruta remota no permitida."]',
    )

    assert runs.last_successful_end(created.id) == base + timedelta(days=3)
    assert runs.has_successful_window(
        created.id,
        window_start_utc=base + timedelta(days=2),
        window_end_utc=base + timedelta(days=3),
    )
    assert not runs.has_successful_window(
        created.id,
        window_start_utc=base + timedelta(days=3),
        window_end_utc=base + timedelta(days=4),
    )
    assert [row["id"] for row in runs.list_runs(status="no_files")] == [
        empty_id
    ]
    assert [row["id"] for row in runs.list_runs(status="no_changes")] == [
        unchanged_id
    ]
    assert [row["id"] for row in runs.list_runs(status="completed")] == [
        completed_id
    ]
    assert {row["id"] for row in runs.list_runs(status="partial")} == {
        failed_file_id,
        errored_id,
        messaged_id,
        warned_id,
    }


def test_run_queue_preserves_remote_timestamp_reliability(
    database: Database,
    repository: ConnectionRepository,
) -> None:
    created = repository.create(connection())
    runs = RunRepository(database)
    run_id = runs.start_run(
        connection_id=created.id,
        trigger="manual",
        window_start_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        window_end_utc=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    source = RemoteFile(
        "/entrada/list.csv",
        100,
        datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        timestamp_reliable=False,
        timestamp_source="LIST",
    )

    runs.add_file(
        run_id=run_id,
        connection_id=created.id,
        remote_file=source,
    )
    claimed = runs.claim_pending_batch(run_id, limit=1)

    assert len(claimed) == 1
    assert claimed[0]["timestamp_reliable"] == 0
    assert claimed[0]["timestamp_source"] == "LIST"


def test_destination_reservations_follow_dynamic_candidates_without_collisions(
    database: Database,
    repository: ConnectionRepository,
    tmp_path: Path,
) -> None:
    created = repository.create(connection())
    runs = RunRepository(database)
    first = (tmp_path / "downloads" / "2026" / "01" / "report.csv").resolve()
    second = (tmp_path / "downloads" / "2026" / "02" / "report.csv").resolve()

    assert runs.reserve_destination(
        connection_id=created.id,
        mapping_scope="same-template",
        remote_path="/entrada/report.csv",
        candidate=first,
    ) == first
    assert runs.reserve_destination(
        connection_id=created.id,
        mapping_scope="same-template",
        remote_path="/entrada/report.csv",
        candidate=second,
    ) == second
    assert runs.reserve_destination(
        connection_id=created.id,
        mapping_scope="changed-and-restored-template",
        remote_path="/entrada/report.csv",
        candidate=first,
    ) == first

    collision = runs.reserve_destination(
        connection_id=created.id,
        mapping_scope="same-template",
        remote_path="/otra/report.csv",
        candidate=first,
    )
    assert collision != first
    assert collision.parent == first.parent
    assert collision.suffix == first.suffix


def test_repository_orders_by_name(repository: ConnectionRepository) -> None:
    repository.create(connection("Zulu"))
    repository.create(replace(connection("alpha"), host="10.0.0.11"))
    assert [item.name for item in repository.list()] == ["alpha", "Zulu"]


def test_recovery_terminalizes_running_runs_and_keeps_staging_reusable(
    database: Database, repository: ConnectionRepository
) -> None:
    saved = repository.create(connection())
    runs = RunRepository(database)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="schedule",
        window_start_utc=datetime(
            2026, 7, 26, tzinfo=timezone.utc
        ),
        window_end_utc=datetime(
            2026, 7, 27, tzinfo=timezone.utc
        ),
    )
    run_file_id = runs.add_file(
        run_id=run_id,
        connection_id=saved.id,
        remote_file=RemoteFile(
            "/entrada/a.csv",
            10,
            datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
        ),
    )
    runs.mark_downloading(run_file_id, attempts=1, bytes_done=5)
    assert runs.recover_interrupted() == (1, 1)
    assert runs.recover_interrupted() == (0, 0)
    with database.connect() as db:
        run = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        file = db.execute(
            "SELECT * FROM run_files WHERE id = ?", (run_file_id,)
        ).fetchone()
    assert run["status"] == "failed"
    assert run["error_type"] == "interrupted"
    assert run["phase"] == "finished"
    assert run["files_found"] == 1
    assert run["files_failed"] == 1
    assert file["status"] == "failed"
    assert file["error_type"] == "interrupted"
    assert file["finished_at"]
