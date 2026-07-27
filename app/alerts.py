"""Alert evaluation, channels, and structural anti-spam."""

from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

from app.db import Database, RunRepository
from app.models import utc_now_iso
from app.logging_setup import redact_secrets
from app.platform.detect import RuntimeMode, runtime_mode
from app.platform.eventlog import write_event
from app.platform.notify_windows import show_notification
from app.settings_store import SettingsStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    run_id: int
    connection_name: str
    cause: str
    title: str
    message: str


class AlertChannel(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


class AlertRepository:
    """Claim alerts transactionally before sending them."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(self, alert: Alert, channel: str) -> int | None:
        try:
            with self.database.connect() as database:
                cursor = database.execute(
                    """
                    INSERT INTO alerts_log(
                        run_id, cause, channel, status, message, created_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        alert.run_id,
                        alert.cause,
                        channel,
                        alert.message,
                        utc_now_iso(),
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def finish(self, alert_id: int, *, status: str, message: str) -> None:
        with self.database.connect() as database:
            database.execute(
                """
                UPDATE alerts_log
                SET status = ?, message = ?
                WHERE id = ?
                """,
                (status, redact_secrets(message), alert_id),
            )

    def list(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self.database.connect() as database:
            rows = database.execute(
                """
                SELECT a.*, c.name AS connection_name
                FROM alerts_log a
                LEFT JOIN runs r ON r.id = a.run_id
                LEFT JOIN connections c ON c.id = r.connection_id
                ORDER BY a.created_at DESC, a.id DESC
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]


class LogChannel:
    name = "log"

    def send(self, alert: Alert) -> None:
        logger.warning(
            "%s [%s, corrida %s]: %s",
            alert.title,
            alert.cause,
            alert.run_id,
            alert.message,
        )


class ToastChannel:
    name = "toast"

    def send(self, alert: Alert) -> None:
        show_notification(alert.title, alert.message)


class EventLogChannel:
    name = "eventlog"

    def send(self, alert: Alert) -> None:
        write_event(f"{alert.title}: {alert.message}")


class WebhookChannel:
    name = "webhook"

    def __init__(self, url: str, *, timeout_s: float = 10.0) -> None:
        self.url = url
        self.timeout_s = timeout_s

    def send(self, alert: Alert) -> None:
        response = httpx.post(
            self.url,
            json={
                "app": "FileHarvester",
                "run_id": alert.run_id,
                "connection": alert.connection_name,
                "cause": alert.cause,
                "title": alert.title,
                "message": alert.message,
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()


class SmtpChannel:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipients: tuple[str, ...],
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        timeout_s: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.recipients = recipients
        self.username = username
        self.password = password
        self.starttls = starttls
        self.timeout_s = timeout_s

    def send(self, alert: Alert) -> None:
        message = EmailMessage()
        message["Subject"] = alert.title
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(alert.message)
        with smtplib.SMTP(
            self.host, self.port, timeout=self.timeout_s
        ) as client:
            if self.starttls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password or "")
            client.send_message(message)


class AlertManager:
    """Evaluate terminal runs and fan out each cause exactly once per channel."""

    def __init__(
        self,
        repository: AlertRepository,
        runs: RunRepository,
        settings: SettingsStore,
        *,
        configured_mode: str,
        channels: tuple[AlertChannel, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.runs = runs
        self.settings = settings
        self.configured_mode = configured_mode
        self._channels = channels

    def evaluate_run(self, run_id: int) -> tuple[Alert, ...]:
        run = self.runs.get_run(run_id)
        alerts = self._causes(run)
        channels = self._channels or self._configured_channels()
        for alert in alerts:
            for channel in channels:
                alert_id = self.repository.claim(alert, channel.name)
                if alert_id is None:
                    continue
                try:
                    channel.send(alert)
                except Exception as exc:
                    self.repository.finish(
                        alert_id,
                        status="failed",
                        message=str(exc),
                    )
                    logger.exception(
                        "Falló el canal de alerta %s para la corrida %s.",
                        channel.name,
                        run_id,
                    )
                else:
                    self.repository.finish(
                        alert_id,
                        status="sent",
                        message=alert.message,
                    )
        return alerts

    def _causes(self, run: dict[str, object]) -> tuple[Alert, ...]:
        connection_name = str(run["connection_name"])
        run_id = int(run["id"])
        status = str(run["status"])
        error_type = str(run["error_type"] or "")
        alerts: list[Alert] = []
        if status == "failed":
            alerts.append(
                Alert(
                    run_id,
                    connection_name,
                    "run_failed",
                    f"FileHarvester: falló {connection_name}",
                    str(run["error_msg"] or "La corrida terminó con error."),
                )
            )
        threshold = int(self.settings.get("alerts.partial_threshold", 1))
        if status == "partial" and int(run["files_failed"] or 0) >= threshold:
            alerts.append(
                Alert(
                    run_id,
                    connection_name,
                    "partial_run",
                    f"FileHarvester: corrida parcial en {connection_name}",
                    f"Fallaron {run['files_failed']} archivo(s).",
                )
            )
        if error_type == "auth":
            alerts.append(
                Alert(
                    run_id,
                    connection_name,
                    "auth_rejected",
                    f"FileHarvester: credencial rechazada en {connection_name}",
                    str(run["error_msg"] or "Revise la credencial configurada."),
                )
            )
        if error_type == "disk_space":
            alerts.append(
                Alert(
                    run_id,
                    connection_name,
                    "disk_space",
                    "FileHarvester: espacio insuficiente",
                    str(run["error_msg"] or "Revise el volumen de destino."),
                )
            )
        if (
            int(run["files_found"] or 0) == 0
            and self._had_previous_files(int(run["connection_id"]), run_id)
        ):
            alerts.append(
                Alert(
                    run_id,
                    connection_name,
                    "suspicious_silence",
                    f"FileHarvester: silencio sospechoso en {connection_name}",
                    "La corrida no encontró archivos, aunque el origen tenía "
                    "actividad en corridas anteriores.",
                )
            )
        return tuple(alerts)

    def _had_previous_files(self, connection_id: int, run_id: int) -> bool:
        with self.repository.database.connect() as database:
            row = database.execute(
                """
                SELECT 1
                FROM runs
                WHERE connection_id = ? AND id < ? AND files_found > 0
                LIMIT 1
                """,
                (connection_id, run_id),
            ).fetchone()
        return row is not None

    def _configured_channels(self) -> tuple[AlertChannel, ...]:
        channels: list[AlertChannel] = [LogChannel()]
        mode = runtime_mode(self.configured_mode)
        if (
            mode == RuntimeMode.INTERACTIVE
            and bool(self.settings.get("alerts.toast.enabled", True))
        ):
            channels.append(ToastChannel())
        if mode == RuntimeMode.HEADLESS:
            channels.append(EventLogChannel())
        if bool(self.settings.get("alerts.webhook.enabled", False)):
            url = os.environ.get("HARVESTER_ALERT_WEBHOOK_URL", "").strip()
            if url:
                channels.append(WebhookChannel(url))
            else:
                logger.warning(
                    "Webhook habilitado sin HARVESTER_ALERT_WEBHOOK_URL."
                )
        if bool(self.settings.get("alerts.smtp.enabled", False)):
            host = str(self.settings.get("alerts.smtp.host", "")).strip()
            sender = str(self.settings.get("alerts.smtp.from", "")).strip()
            recipients = tuple(
                value.strip()
                for value in str(
                    self.settings.get("alerts.smtp.to", "")
                ).split(",")
                if value.strip()
            )
            if host and sender and recipients:
                channels.append(
                    SmtpChannel(
                        host=host,
                        port=int(self.settings.get("alerts.smtp.port", 25)),
                        sender=sender,
                        recipients=recipients,
                        username=(
                            os.environ.get("HARVESTER_SMTP_USER", "").strip()
                            or None
                        ),
                        password=(
                            os.environ.get("HARVESTER_SMTP_PASSWORD", "")
                            or None
                        ),
                        starttls=bool(
                            self.settings.get("alerts.smtp.starttls", False)
                        ),
                    )
                )
            else:
                logger.warning("SMTP habilitado con configuración incompleta.")
        return tuple(channels)
