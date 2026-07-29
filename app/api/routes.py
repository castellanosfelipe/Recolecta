"""Local API for commands, dashboard data, and configuration."""

from __future__ import annotations

import csv
import io
import logging
import threading
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse

from app import __version__
from app.api.schemas import (
    CancelResponse,
    ConnectionCreate,
    ConnectionPatch,
    RunNowRequest,
    RunNowResponse,
    SettingsUpdate,
)
from app.alerts import AlertRepository
from app.connection_import import import_connections
from app.connection_validation import connection_validation_error
from app.db import ConnectionRepository, RunRepository
from app.errors import RecolectaError
from app.exports import ExportService
from app.logging_setup import redact_secrets
from app.models import Connection
from app.orchestrator import DryRunPlan, RunCoordinator
from app.progress import ProgressRegistry
from app.retention import RetentionService
from app.run_logging import RunLogStore
from app.scheduler import SchedulerService, SchedulerSettings
from app.settings_store import SettingsStore
from app.statuses import (
    CONNECTION_STATUS_LABELS,
    enrich_alert,
    enrich_file,
    enrich_plan,
    enrich_run,
)
from app.throttle import ThrottleManager


logger = logging.getLogger(__name__)


def create_router(
    coordinator: RunCoordinator,
    *,
    connections: ConnectionRepository | None = None,
    runs: RunRepository | None = None,
    settings: SettingsStore | None = None,
    progress: ProgressRegistry | None = None,
    scheduler: SchedulerService | None = None,
    run_logs: RunLogStore | None = None,
    alert_repository: AlertRepository | None = None,
    export_service: ExportService | None = None,
    retention: RetentionService | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "app": "Recolecta", "version": __version__}

    @router.post("/api/commands/run-now", response_model=RunNowResponse)
    def run_now(command: RunNowRequest) -> RunNowResponse:
        try:
            if command.connection_id is not None:
                executions = (
                    coordinator.execute_connection(
                        command.connection_id,
                        trigger=command.trigger,
                        selected_date=command.selected_date,
                        dry_run_only=command.dry_run,
                    ),
                )
            else:
                executions = coordinator.execute_all(
                    trigger=command.trigger,
                    selected_date=command.selected_date,
                    dry_run_only=command.dry_run,
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RecolectaError as exc:
            raise HTTPException(
                status_code=409, detail=redact_secrets(exc)
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=redact_secrets(exc)
            ) from exc
        return RunNowResponse(
            executions=[execution.summary() for execution in executions]
        )

    @router.post("/api/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(run_id: int) -> CancelResponse:
        return CancelResponse(
            run_id=run_id,
            cancelled=coordinator.cancel(run_id),
        )

    if not all((connections, runs, settings, progress)):
        return router

    assert connections is not None
    assert runs is not None
    assert settings is not None
    assert progress is not None
    connection_mutation_lock = threading.RLock()

    def reload_scheduler() -> None:
        if scheduler is not None:
            scheduler.configure(SchedulerSettings.load(settings))

    def connection_patch_values(
        payload: ConnectionPatch,
    ) -> tuple[dict[str, object], bool, str | None]:
        values = payload.model_dump(exclude_unset=True)
        secret_was_set = "secret" in values
        secret = values.pop("secret", None)
        nullable_fields = {
            "port",
            "key_path",
            "min_size_bytes",
            "max_size_bytes",
            "bandwidth_limit_kbps",
            "post_action_path",
            "schedule_time",
        }
        filtered = {
            key: value
            for key, value in values.items()
            if value is not None or key in nullable_fields
        }
        return filtered, secret_was_set, secret

    validation_fields = frozenset(
        {
            "name",
            "client",
            "protocol",
            "host",
            "port",
            "username",
            "auth_type",
            "key_path",
            "ssl_mode",
            "remote_paths",
            "recursive",
            "max_depth",
            "dest_root",
            "dest_template",
            "timezone",
            "post_action",
            "post_action_path",
            "timeout_s",
        }
    )
    credential_scope_fields = frozenset(
        {
            "protocol",
            "host",
            "port",
            "username",
            "auth_type",
            "key_path",
        }
    )

    def credential_scope_changed(
        current: Connection,
        draft: Connection,
    ) -> bool:
        return any(
            getattr(current, field) != getattr(draft, field)
            for field in credential_scope_fields
        )

    def validation_secret_for_edit(
        current: Connection,
        draft: Connection,
        *,
        secret_was_set: bool,
        submitted_secret: str | None,
    ) -> str | None:
        if secret_was_set:
            return submitted_secret
        if credential_scope_changed(current, draft) and current.has_secret:
            raise ValueError(
                "Por seguridad, vuelve a ingresar la credencial al cambiar "
                "servidor, puerto, protocolo, usuario o autenticación."
            )
        return connections.get_secret(current.id)

    def connection_requires_validation(
        current: Connection,
        draft: Connection,
        *,
        secret_was_set: bool,
    ) -> bool:
        if secret_was_set:
            return True
        if not current.enabled and draft.enabled:
            return True
        return any(
            getattr(current, field) != getattr(draft, field)
            for field in validation_fields
        )

    @router.get("/api/dashboard")
    def dashboard() -> dict[str, object]:
        summaries = runs.dashboard_summary()
        for item in summaries:
            if item["last_status"]:
                last_run = enrich_run(
                    {
                        "status": item["last_status"],
                        "files_found": item["last_files_found"],
                        "files_downloaded": item["last_files_downloaded"],
                        "files_failed": item["last_files_failed"],
                        "error_type": item["last_error_type"],
                    }
                )
                item["last_result_status"] = last_run["result_status"]
                item["last_status_label"] = last_run["status_label"]
                item["last_status_detail"] = last_run["status_detail"]
            else:
                item["last_result_status"] = "never_run"
                item["last_status_label"] = CONNECTION_STATUS_LABELS["never_run"]
                item["last_status_detail"] = (
                    "Esta conexión todavía no tiene ejecuciones registradas."
                )
            next_run = None
            if scheduler is not None:
                job = scheduler.scheduler.get_job(
                    f"connection-{item['id']}"
                )
                next_run = getattr(job, "next_run_time", None) if job else None
            item["next_run_at"] = next_run.isoformat() if next_run else None
        return {
            "connections": summaries,
            "progress": progress.snapshot(),
        }

    @router.get("/api/progress")
    @router.get("/api/runs/current")
    def current_progress() -> dict[str, object]:
        return progress.snapshot()

    @router.get("/api/connections")
    def list_connections() -> dict[str, object]:
        return {
            "items": [item.to_public_dict() for item in connections.list()]
        }

    @router.post("/api/connections/validate")
    def validate_connection_draft(
        payload: ConnectionPatch,
        connection_id: int | None = Query(default=None, ge=1),
    ) -> dict[str, object]:
        values, secret_was_set, submitted_secret = connection_patch_values(
            payload
        )
        try:
            if connection_id is None:
                create_payload = ConnectionCreate.model_validate(values)
                draft = Connection(
                    **create_payload.model_dump(exclude={"secret"})
                )
                secret = submitted_secret if secret_was_set else None
            else:
                with connection_mutation_lock:
                    current = connections.get(connection_id)
                    draft = current.with_changes(values)
                    secret = validation_secret_for_edit(
                        current,
                        draft,
                        secret_was_set=secret_was_set,
                        submitted_secret=submitted_secret,
                    )
            result = coordinator.validate_connection_draft(
                draft,
                secret=secret,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RecolectaError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(exc),
            ) from exc
        except Exception as exc:
            validation_error = connection_validation_error(exc)
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(validation_error),
            ) from exc
        return result.to_dict()

    @router.post(
        "/api/import/connections",
        status_code=status.HTTP_201_CREATED,
    )
    def import_connection_backup(
        backup: dict[str, Any],
    ) -> dict[str, object]:
        try:
            with connection_mutation_lock:
                result = import_connections(backup, connections)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        reload_scheduler()
        return result.to_dict()

    @router.post(
        "/api/connections",
        status_code=status.HTTP_201_CREATED,
    )
    def create_connection(payload: ConnectionCreate) -> dict[str, object]:
        values = payload.model_dump(exclude={"secret"})
        try:
            with connection_mutation_lock:
                draft = Connection(**values).normalized()
                coordinator.validate_connection_draft(
                    draft,
                    secret=payload.secret,
                )
                created = connections.create(
                    draft,
                    secret=payload.secret,
                )
        except (RecolectaError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(exc),
            ) from exc
        except Exception as exc:
            validation_error = connection_validation_error(exc)
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(validation_error),
            ) from exc
        reload_scheduler()
        return created.to_public_dict()

    @router.get("/api/connections/{connection_id}")
    def get_connection(connection_id: int) -> dict[str, object]:
        try:
            return connections.get(connection_id).to_public_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.patch("/api/connections/{connection_id}")
    def update_connection(
        connection_id: int, payload: ConnectionPatch
    ) -> dict[str, object]:
        values, secret_was_set, secret = connection_patch_values(payload)
        try:
            with connection_mutation_lock:
                current = connections.get(connection_id)
                draft = current.with_changes(values)
                if connection_requires_validation(
                    current,
                    draft,
                    secret_was_set=secret_was_set,
                ):
                    validation_secret = validation_secret_for_edit(
                        current,
                        draft,
                        secret_was_set=secret_was_set,
                        submitted_secret=secret,
                    )
                    coordinator.validate_connection_draft(
                        draft,
                        secret=validation_secret,
                    )
                if secret_was_set:
                    updated = connections.update(
                        connection_id, values, secret=secret
                    )
                else:
                    updated = connections.update(connection_id, values)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RecolectaError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(exc),
            ) from exc
        except Exception as exc:
            validation_error = connection_validation_error(exc)
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(validation_error),
            ) from exc
        reload_scheduler()
        return updated.to_public_dict()

    @router.delete(
        "/api/connections/{connection_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_connection(connection_id: int) -> Response:
        with connection_mutation_lock:
            deleted = connections.delete(connection_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"No existe la conexión {connection_id}.",
            )
        reload_scheduler()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/api/connections/{connection_id}/duplicate",
        status_code=status.HTTP_201_CREATED,
    )
    def duplicate_connection(connection_id: int) -> dict[str, object]:
        try:
            with connection_mutation_lock:
                source = connections.get(connection_id)
                values = source.to_public_dict()
                for key in (
                    "id",
                    "has_secret",
                    "created_at",
                    "updated_at",
                ):
                    values.pop(key, None)
                values["name"] = f"{source.name} (copia)"
                values["enabled"] = False
                duplicate = connections.create(Connection(**values))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        reload_scheduler()
        return duplicate.to_public_dict()

    @router.post("/api/connections/{connection_id}/test")
    @router.post("/api/connections/{connection_id}/dry-run")
    def test_connection(
        connection_id: int,
        selected_date: date | None = None,
    ) -> dict[str, object]:
        try:
            execution = coordinator.execute_connection(
                connection_id,
                trigger="manual",
                selected_date=selected_date,
                dry_run_only=True,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RecolectaError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail=redact_secrets(exc)
            ) from exc
        except Exception as exc:
            validation_error = connection_validation_error(exc)
            raise HTTPException(
                status_code=422,
                detail=redact_secrets(validation_error),
            ) from exc
        return _plan_response(execution.plan)

    @router.post(
        "/api/connections/{connection_id}/run",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_connection_run(
        connection_id: int,
        selected_date: date | None = None,
    ) -> dict[str, object]:
        try:
            connection = connections.get(connection_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not connection.enabled:
            raise HTTPException(
                status_code=409,
                detail=f"La conexión {connection.name} está en pausa.",
            )

        def execute() -> None:
            try:
                coordinator.execute_connection(
                    connection_id,
                    trigger="manual",
                    selected_date=selected_date,
                )
            except Exception:
                logger.exception(
                    "La corrida manual de la conexión %s falló.",
                    connection_id,
                )

        threading.Thread(
            target=execute,
            name=f"manual-run-{connection_id}",
            daemon=True,
        ).start()
        return {"accepted": True, "connection_id": connection_id}

    @router.get("/api/runs")
    def list_runs(
        connection_id: int | None = None,
        run_status: str | None = Query(default=None, alias="status"),
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return {
            "items": [
                enrich_run(row)
                for row in runs.list_runs(
                    connection_id=connection_id,
                    status=run_status,
                    date_from=(
                        date_from.isoformat() if date_from else None
                    ),
                    date_to=date_to.isoformat() if date_to else None,
                    limit=limit,
                    offset=offset,
                )
            ]
        }

    @router.get("/api/runs/{run_id}")
    def get_run(run_id: int) -> dict[str, object]:
        try:
            run = runs.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = enrich_run(run)
        result["files"] = _all_files(runs, run_id=run_id)
        return result

    @router.get("/api/files")
    def list_files(
        run_id: int | None = None,
        connection_id: int | None = None,
        file_status: str | None = Query(default=None, alias="status"),
        search: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return {
            "items": [
                enrich_file(row)
                for row in runs.list_files(
                    run_id=run_id,
                    connection_id=connection_id,
                    status=file_status,
                    search=search,
                    date_from=(
                        date_from.isoformat() if date_from else None
                    ),
                    date_to=date_to.isoformat() if date_to else None,
                    limit=limit,
                    offset=offset,
                )
            ]
        }

    @router.get("/api/files/export.csv")
    def export_files() -> Response:
        rows = _all_files(runs)
        stream = io.StringIO()
        fields = [
            "id",
            "run_id",
            "connection_name",
            "remote_path",
            "local_path",
            "size_bytes",
            "bytes_done",
            "mtime_utc",
            "sha256",
            "status",
            "status_label",
            "attempts",
            "error_type",
            "error_msg",
            "duration_s",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            stream.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="recolecta-files.csv"'
            },
        )

    if export_service is not None:

        @router.get("/api/export/files.csv")
        def export_files_filtered(
            run_id: int | None = None,
            connection_id: int | None = None,
            date_from: date | None = Query(default=None, alias="from"),
            date_to: date | None = Query(default=None, alias="to"),
            file_status: str | None = Query(default=None, alias="status"),
        ) -> Response:
            return Response(
                export_service.files_csv(
                    run_id=run_id,
                    connection_id=connection_id,
                    date_from=(
                        date_from.isoformat() if date_from else None
                    ),
                    date_to=date_to.isoformat() if date_to else None,
                    status=file_status,
                ),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        'attachment; filename="recolecta-files.csv"'
                    )
                },
            )

        @router.get("/api/export/runs.csv")
        def export_runs_csv(
            days: int = Query(default=30, ge=1, le=3650),
        ) -> Response:
            return Response(
                export_service.runs_csv(days=days),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        'attachment; filename="recolecta-runs.csv"'
                    )
                },
            )

        @router.get("/api/export/report.html", response_class=HTMLResponse)
        def export_html_report(
            days: int = Query(default=30, ge=1, le=3650),
            client: str | None = None,
        ) -> HTMLResponse:
            return HTMLResponse(
                export_service.html_report(days=days, client=client),
                headers={
                    "Content-Disposition": (
                        'attachment; filename="recolecta-report.html"'
                    )
                },
            )

        @router.get("/api/export/bundle.zip")
        def export_bundle(
            days: int = Query(default=7, ge=1, le=3650),
        ) -> FileResponse:
            path = export_service.support_bundle(days=days)
            return FileResponse(
                path,
                media_type="application/zip",
                filename=path.name,
            )

    if run_logs is not None:

        @router.get("/api/runs/{run_id}/log.jsonl")
        def download_run_log(run_id: int) -> FileResponse:
            try:
                runs.get_run(run_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            path = run_logs.find(run_id)
            if path is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"La corrida {run_id} no tiene log JSONL.",
                )
            return FileResponse(
                path,
                media_type="application/x-ndjson",
                filename=path.name,
            )

    if alert_repository is not None:

        @router.get("/api/alerts")
        def list_alerts(
            limit: int = Query(default=100, ge=1, le=500),
        ) -> dict[str, object]:
            return {
                "items": [
                    enrich_alert(row)
                    for row in alert_repository.list(limit=limit)
                ]
            }

    if retention is not None:

        @router.post("/api/retention/run")
        def run_retention() -> dict[str, object]:
            days = int(settings.get("retention.days", 180))
            return retention.purge(days=days).__dict__

    @router.get("/api/settings")
    def get_settings() -> dict[str, object]:
        return {"values": _settings_for_ui(_public_settings(settings.all()))}

    @router.put("/api/settings")
    def update_settings(payload: SettingsUpdate) -> dict[str, object]:
        values = dict(payload.values)
        forbidden = [
            key for key in values if _sensitive_setting_key(key)
        ]
        if forbidden:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Los secretos de alertas deben configurarse mediante "
                    "variables de entorno."
                ),
            )
        daily_time = values.get("schedule.daily_time")
        if daily_time is not None:
            try:
                hour_text, minute_text = str(daily_time).split(":", 1)
                values["schedule.hour"] = int(hour_text)
                values["schedule.minute"] = int(minute_text)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="La hora diaria debe usar el formato HH:MM.",
                ) from exc
        if "schedule.jitter_s" in values:
            try:
                jitter_s = int(values["schedule.jitter_s"])
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="El jitter debe ser un número entero de segundos.",
                ) from exc
            if jitter_s < 0:
                raise HTTPException(
                    status_code=422,
                    detail="El jitter no puede ser negativo.",
                )
            values["schedule.jitter_minutes"] = round(jitter_s / 60)

        projected = settings.all()
        projected.update(values)
        try:
            schedule_settings = SchedulerSettings(
                hour=int(projected.get("schedule.hour", 2)),
                minute=int(projected.get("schedule.minute", 0)),
                jitter_minutes=int(
                    projected.get("schedule.jitter_minutes", 0)
                ),
                catchup_enabled=bool(
                    projected.get("catchup.enabled", True)
                ),
                catchup_max_days=int(
                    projected.get("catchup.max_days", 3)
                ),
                startup_delay_s=int(
                    projected.get("catchup.startup_delay_s", 60)
                ),
                retention_days=int(projected.get("retention.days", 180)),
            ).validated()
            global_parallelism = int(
                projected.get("concurrency.global", 4)
            )
            global_bandwidth = int(
                projected.get("bandwidth.global_kbps", 0)
            )
            reserve_percent = float(
                projected.get("disk.reserve_percent", 10)
            )
            minimum_spacing = float(
                projected.get("courtesy.minimum_spacing_s", 0)
            )
            partial_threshold = int(
                projected.get("alerts.partial_threshold", 1)
            )
            smtp_port = int(projected.get("alerts.smtp.port", 25))
            if global_parallelism < 1:
                raise ValueError(
                    "concurrency.global debe ser al menos uno."
                )
            if global_bandwidth < 0:
                raise ValueError(
                    "bandwidth.global_kbps no puede ser negativo."
                )
            if not 0 <= reserve_percent <= 100:
                raise ValueError(
                    "disk.reserve_percent debe estar entre 0 y 100."
                )
            if minimum_spacing < 0:
                raise ValueError(
                    "courtesy.minimum_spacing_s no puede ser negativo."
                )
            if partial_threshold < 1:
                raise ValueError(
                    "alerts.partial_threshold debe ser al menos uno."
                )
            if not 1 <= smtp_port <= 65535:
                raise ValueError(
                    "alerts.smtp.port debe estar entre 1 y 65535."
                )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for key, value in values.items():
            settings.set(key, value)
        if scheduler is not None:
            scheduler.configure(schedule_settings)
        coordinator.throttle = ThrottleManager(
            global_parallelism=global_parallelism
        )
        coordinator.global_bandwidth_limit_kbps = (
            global_bandwidth or None
        )
        coordinator.reserve_ratio = reserve_percent / 100
        coordinator.minimum_spacing_s = minimum_spacing
        return {
            "values": _settings_for_ui(_public_settings(settings.all()))
        }

    return router


def _plan_response(plan: DryRunPlan) -> dict[str, object]:
    return enrich_plan({
        "connection_id": plan.connection_id,
        "window_start_utc": plan.window.start_utc.isoformat(),
        "window_end_utc": plan.window.end_utc.isoformat(),
        "files_to_download": len(plan.files_to_download),
        "is_partial": plan.is_partial,
        "warnings": list(plan.warnings),
        "counters": plan.counters,
        "items": [
            {
                "remote_path": item.file.remote_path,
                "size_bytes": item.file.size_bytes,
                "mtime_utc": (
                    item.file.mtime_utc.isoformat()
                    if item.file.mtime_utc is not None
                    else None
                ),
                "status": item.status.value,
                "reason": item.reason,
            }
            for item in plan.items
        ],
    })


def _settings_for_ui(values: dict[str, object]) -> dict[str, object]:
    result = dict(values)
    if "schedule.daily_time" not in result:
        hour = int(result.get("schedule.hour", 2))
        minute = int(result.get("schedule.minute", 0))
        result["schedule.daily_time"] = f"{hour:02d}:{minute:02d}"
    if "schedule.jitter_s" not in result:
        result["schedule.jitter_s"] = (
            int(result.get("schedule.jitter_minutes", 0)) * 60
        )
    return result


def _all_files(
    runs: RunRepository, *, run_id: int | None = None
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    offset = 0
    while True:
        page = runs.list_files(run_id=run_id, limit=1000, offset=offset)
        rows.extend(enrich_file(row) for row in page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def _public_settings(values: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if not _sensitive_setting_key(key)
    }


def _sensitive_setting_key(key: str) -> bool:
    normalized = key.casefold()
    return (
        any(
            token in normalized
            for token in (
                "password",
                "secret",
                "passphrase",
                "token",
                "credential",
            )
        )
        or normalized == "alerts.webhook.url"
    )
