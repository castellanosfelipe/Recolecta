import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from app import alerts as alerts_module
from app.alerts import (
    Alert,
    AlertManager,
    AlertRepository,
    EventLogChannel,
    LogChannel,
    SmtpChannel,
    ToastChannel,
    WebhookChannel,
)
from app.db import ConnectionRepository, Database, RunRepository
from app.models import Connection
from app.platform.detect import RuntimeMode
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


def test_builtin_channels_deliver_expected_payloads(
    monkeypatch, caplog
) -> None:
    alert = Alert(
        17,
        "Origen",
        "disk_space",
        "Espacio insuficiente",
        "Libere espacio.",
    )
    notifications: list[tuple[str, str]] = []
    events: list[str] = []
    posts: list[dict[str, object]] = []

    monkeypatch.setattr(
        alerts_module,
        "show_notification",
        lambda title, message: notifications.append((title, message)),
    )
    monkeypatch.setattr(
        alerts_module, "write_event", lambda message: events.append(message)
    )

    class Response:
        def raise_for_status(self) -> None:
            posts.append({"raised": True})

    def post(url, *, json, timeout):
        posts.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr(alerts_module.httpx, "post", post)

    smtp_calls: list[tuple[str, object]] = []

    class FakeSmtp:
        def __init__(self, host, port, *, timeout):
            smtp_calls.append(("connect", (host, port, timeout)))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self):
            smtp_calls.append(("starttls", None))

        def login(self, username, password):
            smtp_calls.append(("login", (username, password)))

        def send_message(self, message):
            smtp_calls.append(("message", message))

    monkeypatch.setattr(alerts_module.smtplib, "SMTP", FakeSmtp)

    with caplog.at_level(logging.WARNING):
        LogChannel().send(alert)
    ToastChannel().send(alert)
    EventLogChannel().send(alert)
    WebhookChannel("https://alerts.test/hook", timeout_s=3).send(alert)
    SmtpChannel(
        host="smtp.test",
        port=587,
        sender="harvester@test",
        recipients=("ops@test",),
        username="operator",
        password="secret",
        starttls=True,
        timeout_s=4,
    ).send(alert)

    assert "Espacio insuficiente" in caplog.text
    assert notifications == [("Espacio insuficiente", "Libere espacio.")]
    assert events == ["Espacio insuficiente: Libere espacio."]
    assert posts[0]["json"]["cause"] == "disk_space"
    assert posts[1] == {"raised": True}
    assert [call[0] for call in smtp_calls] == [
        "connect",
        "starttls",
        "login",
        "message",
    ]
    message = smtp_calls[-1][1]
    assert message["To"] == "ops@test"
    assert "Libere espacio." in message.get_content()


class DictSettings:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


def test_configured_channels_follow_runtime_and_environment(
    monkeypatch, caplog
) -> None:
    settings = DictSettings(
        {
            "alerts.toast.enabled": True,
            "alerts.webhook.enabled": True,
            "alerts.smtp.enabled": True,
            "alerts.smtp.host": "smtp.test",
            "alerts.smtp.from": "harvester@test",
            "alerts.smtp.to": "ops@test, audit@test",
            "alerts.smtp.port": 2525,
            "alerts.smtp.starttls": True,
        }
    )
    monkeypatch.setenv(
        "HARVESTER_ALERT_WEBHOOK_URL", "https://alerts.test/hook"
    )
    monkeypatch.setenv("HARVESTER_SMTP_USER", "operator")
    monkeypatch.setenv("HARVESTER_SMTP_PASSWORD", "secret")
    monkeypatch.setattr(
        alerts_module,
        "runtime_mode",
        lambda configured: RuntimeMode.INTERACTIVE,
    )
    manager = AlertManager(
        None,
        None,
        settings,
        configured_mode="windows",
    )

    channels = manager._configured_channels()

    assert [channel.name for channel in channels] == [
        "log",
        "toast",
        "webhook",
        "smtp",
    ]
    smtp = channels[-1]
    assert smtp.recipients == ("ops@test", "audit@test")
    assert smtp.port == 2525
    assert smtp.starttls

    incomplete = DictSettings(
        {
            "alerts.webhook.enabled": True,
            "alerts.smtp.enabled": True,
        }
    )
    monkeypatch.delenv("HARVESTER_ALERT_WEBHOOK_URL")
    monkeypatch.setattr(
        alerts_module,
        "runtime_mode",
        lambda configured: RuntimeMode.HEADLESS,
    )
    headless = AlertManager(
        None,
        None,
        incomplete,
        configured_mode="service",
    )
    with caplog.at_level(logging.WARNING):
        headless_channels = headless._configured_channels()

    assert [channel.name for channel in headless_channels] == [
        "log",
        "eventlog",
    ]
    assert "Webhook habilitado" in caplog.text
    assert "SMTP habilitado" in caplog.text


def test_disk_space_alert_and_repository_listing(tmp_path: Path) -> None:
    database, saved, runs, settings = build_data(tmp_path)
    repository = AlertRepository(database)
    manager = AlertManager(
        repository,
        runs,
        settings,
        configured_mode="dev",
        channels=(Channel(),),
    )
    run_id = start_run(runs, saved.id, 1)
    runs.finish_run(
        run_id,
        status="failed",
        error_type="disk_space",
        error_msg="Volumen lleno.",
    )

    assert {item.cause for item in manager.evaluate_run(run_id)} == {
        "run_failed",
        "disk_space",
    }
    listed = repository.list(limit=0)
    assert len(listed) == 1
    assert {item["connection_name"] for item in listed} == {"Origen"}
