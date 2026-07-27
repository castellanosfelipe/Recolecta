import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import MIGRATIONS, ConnectionRepository, Database, RunRepository
from app.models import Connection, Protocol
from app.platform.secrets_fernet import FernetSecretStore
from app.transports.base import RemoteFile


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "data" / "harvester.db")
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
            "enabled": False,
        },
    )
    assert updated.name == "SFTP Renombrado"
    assert updated.protocol == Protocol.FTP
    assert updated.port == 21
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


def test_successful_file_identity_is_unique(
    database: Database, repository: ConnectionRepository
) -> None:
    created = repository.create(connection())
    with database.connect() as db:
        run_id = db.execute(
            """
            INSERT INTO runs(
                connection_id, trigger, window_start_utc, window_end_utc,
                started_at, status
            ) VALUES (?, 'manual', '2026-01-01', '2026-01-02', '2026-01-02', 'ok')
            """,
            (created.id,),
        ).lastrowid
        values = (
            run_id,
            created.id,
            "/entrada/a.csv",
            100,
            "2026-01-01T12:00:00+00:00",
            "ok",
        )
        db.execute(
            """
            INSERT INTO run_files(
                run_id, connection_id, remote_path, size_bytes, mtime_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO run_files(
                    run_id, connection_id, remote_path, size_bytes, mtime_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )


def test_repository_orders_by_name(repository: ConnectionRepository) -> None:
    repository.create(connection("Zulu"))
    repository.create(replace(connection("alpha"), host="10.0.0.11"))
    assert [item.name for item in repository.list()] == ["alpha", "Zulu"]


def test_recovery_fails_running_runs_and_returns_downloads_to_pending(
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
    assert file["status"] == "pending"
    assert file["error_type"] == "interrupted"
