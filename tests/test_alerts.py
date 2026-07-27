from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app.alerts import AlertManager, AlertRepository
from app.db import ConnectionRepository, Database, RunRepository
from app.models import Connection
from app.platform.secrets_fernet import FernetSecretStore
from app.settings_store import SettingsStore
from app.transports.base import RemoteFile


class Channel:
    name = "test"

    def __init__(self, *, fail: bool = False) -> None:
        self.alerts = []
        self.fail = fail

    def send(self, alert) -> None:
        self.alerts.append(alert)
        if self.fail:
            raise RuntimeError("canal caído")


def build_data(tmp_path: Path):
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(name="Origen", host="example.test", remote_paths=("/in",))
    )
    return (
        database,
        saved,
        RunRepository(database),
        SettingsStore(database),
    )


def start_run(runs: RunRepository, connection_id: int, index: int) -> int:
    now = datetime(2026, 7, 27, tzinfo=timezone.utc) + timedelta(minutes=index)
    return runs.start_run(
        connection_id=connection_id,
        trigger="manual",
        window_start_utc=now - timedelta(days=1),
        window_end_utc=now,
        started_at=now,
    )


def test_alert_causes_and_structural_antispam(tmp_path: Path) -> None:
    database, saved, runs, settings = build_data(tmp_path)
    channel = Channel()
    manager = AlertManager(
        AlertRepository(database),
        runs,
        settings,
        configured_mode="dev",
        channels=(channel,),
    )
    run_id = start_run(runs, saved.id, 1)
    runs.finish_run(
        run_id,
        status="failed",
        error_type="auth",
        error_msg="Credencial rechazada.",
    )
    causes = {alert.cause for alert in manager.evaluate_run(run_id)}
    manager.evaluate_run(run_id)
    assert causes == {"run_failed", "auth_rejected"}
    assert len(channel.alerts) == 2
    with database.connect() as db:
        rows = db.execute(
            "SELECT cause, status FROM alerts_log ORDER BY cause"
        ).fetchall()
    assert [(row["cause"], row["status"]) for row in rows] == [
        ("auth_rejected", "sent"),
        ("run_failed", "sent"),
    ]


def test_partial_silence_and_failed_channel_are_recorded(tmp_path: Path) -> None:
    database, saved, runs, settings = build_data(tmp_path)
    previous = start_run(runs, saved.id, 1)
    runs.add_file(
        run_id=previous,
        connection_id=saved.id,
        remote_file=RemoteFile("/in/old.csv", 10, None),
        status="skipped",
    )
    runs.finish_run(previous, status="ok")

    failed_channel = Channel(fail=True)
    manager = AlertManager(
        AlertRepository(database),
        runs,
        settings,
        configured_mode="dev",
        channels=(failed_channel,),
    )
    empty = start_run(runs, saved.id, 2)
    runs.finish_run(empty, status="ok")
    alerts = manager.evaluate_run(empty)
    assert [alert.cause for alert in alerts] == ["suspicious_silence"]
    with database.connect() as db:
        row = db.execute(
            "SELECT status, message FROM alerts_log WHERE run_id = ?",
            (empty,),
        ).fetchone()
    assert row["status"] == "failed"
    assert "canal caído" in row["message"]

    partial = start_run(runs, saved.id, 3)
    runs.add_file(
        run_id=partial,
        connection_id=saved.id,
        remote_file=RemoteFile("/in/broken.csv", 10, None),
        status="pending",
    )
    runs.fail_unfinished(
        partial,
        error_type="protocol",
        error_msg="Transferencia incompleta.",
    )
    runs.finish_run(partial, status="partial")
    assert [alert.cause for alert in manager.evaluate_run(partial)] == [
        "partial_run"
    ]
