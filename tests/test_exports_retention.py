import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import AppPaths
from app.db import ConnectionRepository, Database, RunRepository
from app.exports import ExportService
from app.models import Connection
from app.platform.secrets_fernet import FernetSecretStore
from app.retention import RetentionService
from app.run_logging import RunLogStore
from app.settings_store import SettingsStore
from app.transports.base import RemoteFile


def build_data(tmp_path: Path):
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(
            name="Auditoría",
            client="Cliente A",
            host="example.test",
            remote_paths=("/in",),
        ),
        secret="muy-secreto",
    )
    runs = RunRepository(database)
    settings = SettingsStore(database)
    settings.set("alerts.smtp.password", "no-exportar")
    settings.set("retention.days", 180)
    logs = RunLogStore(paths.run_logs)
    return paths, database, connections, saved, runs, settings, logs


def test_support_bundle_report_and_csv_are_safe(tmp_path: Path) -> None:
    paths, _, connections, saved, runs, settings, logs = build_data(tmp_path)
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=now - timedelta(days=1),
        window_end_utc=now,
        started_at=now,
    )
    runs.add_file(
        run_id=run_id,
        connection_id=saved.id,
        remote_file=RemoteFile("=formula.csv", 10, None),
        status="skipped",
    )
    runs.finish_run(run_id, status="ok")
    logs.create(
        run_id=run_id,
        connection_name=saved.name,
        started_at=now,
    ).write("run_finished", status="ok")
    (paths.logs / "app.log").write_text("registro seguro", encoding="utf-8")
    service = ExportService(
        paths=paths,
        runs=runs,
        connections=connections,
        settings=settings,
        run_logs=logs,
        now=lambda: now,
    )
    assert "'=formula.csv" in service.files_csv()
    assert "Cliente A" in service.html_report(days=30, client="Cliente A")
    configuration = json.dumps(service.safe_configuration())
    assert "muy-secreto" not in configuration
    assert "no-exportar" not in configuration
    bundle = service.support_bundle(days=7)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        content = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in names
        )
    assert {
        "exports/runs.csv",
        "exports/files.csv",
        "exports/configuration.json",
        "exports/report.html",
        "logs/app.log",
    }.issubset(names)
    assert any(name.startswith("logs/runs/") for name in names)
    assert "muy-secreto" not in content
    assert "no-exportar" not in content


def test_retention_removes_only_old_audit_data(tmp_path: Path) -> None:
    paths, database, _, saved, runs, _, _ = build_data(tmp_path)
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    old = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=now - timedelta(days=301),
        window_end_utc=now - timedelta(days=300),
        started_at=now - timedelta(days=300),
    )
    runs.add_file(
        run_id=old,
        connection_id=saved.id,
        remote_file=RemoteFile("/old.csv", 1, None),
        status="skipped",
    )
    runs.finish_run(old, status="ok")
    current = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=now - timedelta(days=1),
        window_end_utc=now,
        started_at=now,
    )
    runs.finish_run(current, status="ok")

    old_log = paths.run_logs / "old.jsonl"
    old_log.write_text("{}\n", encoding="utf-8")
    old_export = paths.exports / "old.zip"
    old_export.write_bytes(b"zip")
    timestamp = (now - timedelta(days=300)).timestamp()
    os.utime(old_log, (timestamp, timestamp))
    os.utime(old_export, (timestamp, timestamp))
    downloaded = paths.downloads / "must-stay.bin"
    downloaded.write_bytes(b"data")

    result = RetentionService(
        database,
        run_logs=paths.run_logs,
        exports=paths.exports,
        now=lambda: now,
    ).purge(days=180)
    assert result.runs_deleted == 1
    assert result.files_deleted == 1
    assert result.logs_deleted == 1
    assert result.exports_deleted == 1
    assert downloaded.read_bytes() == b"data"
    assert runs.get_run(current)["status"] == "ok"
