import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.config import AppConfig, AppPaths
from app.db import ConnectionRepository, Database
from app.downloader import StagingCleanupResult
from app.main import _cleanup_runtime_staging, build_runtime
from app.models import Connection
from app.platform.secrets_fernet import FernetSecretStore
from app.settings_store import SettingsStore


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        port=8091,
        bind_lan=False,
        dashboard_user=None,
        dashboard_password=None,
        mode="dev",
        paths=AppPaths.from_root(tmp_path).ensure(),
    )


def _connection(name: str, destination: str) -> Connection:
    return Connection(
        name=name,
        host="example.test",
        remote_paths=("/entrada",),
        dest_root=destination,
    ).normalized()


def test_build_runtime_cleans_each_destination_once_with_catchup_retention(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    database = Database(config.paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    connections.create(_connection("Primera", "shared"))
    connections.create(_connection("Segunda", "shared"))
    SettingsStore(database).set("catchup.max_days", 9)
    calls: list[tuple[Path, set[str], datetime]] = []

    def cleanup(staging, *, active_part_names, cutoff):
        calls.append((staging, active_part_names, cutoff))
        return StagingCleanupResult()

    monkeypatch.setattr("app.main.cleanup_orphaned_staging", cleanup)
    before = datetime.now(timezone.utc)

    runtime = build_runtime(config)

    after = datetime.now(timezone.utc)
    assert len(calls) == 1
    assert calls[0][0] == (tmp_path / "shared" / ".staging").resolve()
    assert calls[0][1] == set()
    assert before - timedelta(days=10) <= calls[0][2]
    assert calls[0][2] <= after - timedelta(days=10)
    assert runtime.recovered_runs == 0
    assert runtime.recovered_files == 0


def test_runtime_cleanup_warns_and_continues_after_destination_errors(
    monkeypatch,
    tmp_path: Path,
    caplog,
) -> None:
    connections = SimpleNamespace(
        list=lambda: [
            SimpleNamespace(dest_root="blocked"),
            SimpleNamespace(dest_root="reported"),
            SimpleNamespace(dest_root="healthy"),
        ]
    )
    visited: list[str] = []

    def cleanup(staging, *, active_part_names, cutoff):
        visited.append(staging.parent.name)
        if staging.parent.name == "blocked":
            raise PermissionError("sin acceso")
        if staging.parent.name == "reported":
            return StagingCleanupResult(errors=2)
        return StagingCleanupResult(files_removed=1, bytes_removed=4)

    monkeypatch.setattr("app.main.cleanup_orphaned_staging", cleanup)
    caplog.set_level(logging.INFO, logger="app.main")

    _cleanup_runtime_staging(
        connections,
        portable_root=tmp_path,
        catchup_max_days=3,
        now=datetime(2026, 7, 29, tzinfo=timezone.utc),
    )

    assert visited == ["blocked", "reported", "healthy"]
    assert "el arranque continuará" in caplog.text
    assert "2 errores" in caplog.text
    assert "1 parciales y 4 bytes" in caplog.text
