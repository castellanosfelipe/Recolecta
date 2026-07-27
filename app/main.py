"""Resident application lifecycle: database, scheduler, and local API."""

from __future__ import annotations

import logging
import base64
import secrets
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.alerts import AlertManager, AlertRepository
from app.api.routes import create_router
from app.config import AppConfig
from app.db import ConnectionRepository, Database, RunRepository
from app.exports import ExportService
from app.logging_setup import configure_logging
from app.orchestrator import RunCoordinator
from app.platform.secretstore import create_secret_store
from app.platform.single_instance import SingleInstance
from app.platform.tray_windows import create_tray
from app.progress import ProgressRegistry
from app.run_logging import RunLogStore
from app.retention import RetentionService
from app.scheduler import SchedulerService, SchedulerSettings
from app.settings_store import SettingsStore
from app.throttle import ThrottleManager


logger = logging.getLogger(__name__)


@dataclass
class RuntimeComponents:
    database: Database
    connections: ConnectionRepository
    runs: RunRepository
    settings: SettingsStore
    progress: ProgressRegistry
    run_logs: RunLogStore
    alert_repository: AlertRepository
    alerts: AlertManager
    export_service: ExportService
    retention: RetentionService
    coordinator: RunCoordinator
    scheduler: SchedulerService
    recovered_runs: int
    recovered_files: int


def build_runtime(config: AppConfig) -> RuntimeComponents:
    database = Database(config.paths.database)
    database.initialize()
    secret_store = create_secret_store(config.mode, config.paths.data)
    connections = ConnectionRepository(database, secret_store)
    runs = RunRepository(database)
    recovered_runs, recovered_files = runs.recover_interrupted()
    settings = SettingsStore(database)
    global_parallelism = int(settings.get("concurrency.global", 4))
    throttle = ThrottleManager(global_parallelism=global_parallelism)
    progress = ProgressRegistry(persist_progress=runs.update_file_progress)
    run_logs = RunLogStore(config.paths.run_logs)
    alert_repository = AlertRepository(database)
    alerts = AlertManager(
        alert_repository,
        runs,
        settings,
        configured_mode=config.mode,
    )
    export_service = ExportService(
        paths=config.paths,
        runs=runs,
        connections=connections,
        settings=settings,
        run_logs=run_logs,
    )
    retention = RetentionService(
        database,
        run_logs=config.paths.run_logs,
        exports=config.paths.exports,
    )
    coordinator = RunCoordinator(
        database,
        connections,
        config.paths,
        throttle=throttle,
        progress=progress,
        run_logs=run_logs,
        alerts=alerts,
        minimum_spacing_s=float(
            settings.get("courtesy.minimum_spacing_s", 0)
        ),
        reserve_ratio=float(settings.get("disk.reserve_percent", 10)) / 100,
        global_bandwidth_limit_kbps=(
            int(settings.get("bandwidth.global_kbps", 0)) or None
        ),
    )
    scheduler = SchedulerService(
        coordinator,
        connections,
        runs,
        retention_callback=lambda days: retention.purge(days=days),
    )
    scheduler.configure(SchedulerSettings.load(settings))
    return RuntimeComponents(
        database,
        connections,
        runs,
        settings,
        progress,
        run_logs,
        alert_repository,
        alerts,
        export_service,
        retention,
        coordinator,
        scheduler,
        recovered_runs,
        recovered_files,
    )


def create_app(config: AppConfig, runtime: RuntimeComponents | None = None) -> FastAPI:
    components = runtime or build_runtime(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if components.recovered_runs:
            logger.warning(
                "Se recuperaron %s corridas y %s archivos interrumpidos.",
                components.recovered_runs,
                components.recovered_files,
            )
        components.scheduler.start()
        catchup_thread = threading.Thread(
            target=components.scheduler.run_catchup,
            name="harvester-catchup",
            daemon=True,
        )
        catchup_thread.start()
        yield
        components.scheduler.shutdown(wait=False)

    app = FastAPI(
        title="FileHarvester",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = components
    app.include_router(
        create_router(
            components.coordinator,
            connections=components.connections,
            runs=components.runs,
            settings=components.settings,
            progress=components.progress,
            scheduler=components.scheduler,
            run_logs=components.run_logs,
            alert_repository=components.alert_repository,
            export_service=components.export_service,
            retention=components.retention,
        )
    )
    resources = _resource_root()
    app.mount(
        "/static",
        StaticFiles(directory=resources / "static"),
        name="static",
    )

    @app.get("/", include_in_schema=False)
    def dashboard_index() -> FileResponse:
        return FileResponse(resources / "templates" / "index.html")

    if config.dashboard_user and config.dashboard_password:
        expected_user = config.dashboard_user
        expected_password = config.dashboard_password

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path == "/healthz":
                return await call_next(request)
            supplied = request.headers.get("Authorization", "")
            valid = False
            if supplied.startswith("Basic "):
                try:
                    decoded = base64.b64decode(
                        supplied[6:], validate=True
                    ).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    valid = secrets.compare_digest(
                        username, expected_user
                    ) and secrets.compare_digest(password, expected_password)
                except (ValueError, UnicodeDecodeError):
                    valid = False
            if not valid:
                return JSONResponse(
                    {"detail": "Autenticación requerida."},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="FileHarvester"'},
                )
            return await call_next(request)
    return app


def run_resident(config: AppConfig | None = None) -> int:
    resolved = config or AppConfig.from_env()
    configure_logging(resolved.paths.logs)
    if resolved.bind_lan:
        if resolved.dashboard_user:
            logger.warning(
                "El dashboard está expuesto a la LAN con Basic Auth."
            )
        else:
            logger.warning(
                "El dashboard está expuesto a la LAN sin autenticación. "
                "Configure HARVESTER_DASH_USER y HARVESTER_DASH_PASS."
            )
    guard = SingleInstance(resolved.paths.data)
    if not guard.try_acquire():
        logger.error("FileHarvester ya se está ejecutando.")
        return 2
    try:
        app = create_app(resolved)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=resolved.host,
                port=resolved.port,
                log_config=None,
                access_log=False,
            )
        )
        runtime = app.state.runtime
        tray = create_tray(
            configured_mode=resolved.mode,
            dashboard_url=f"http://127.0.0.1:{resolved.port}",
            run_all=lambda: runtime.coordinator.execute_all(trigger="manual"),
            shutdown=lambda: setattr(server, "should_exit", True),
            status_provider=lambda: _tray_status(runtime),
        )
        if tray is not None:
            tray.start()
        try:
            server.run()
        finally:
            if tray is not None:
                tray.stop()
        return 0
    finally:
        guard.release()


def _resource_root() -> Path:
    """Resolve bundled web assets in source and PyInstaller modes."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def _tray_status(runtime: RuntimeComponents) -> str:
    if runtime.progress.snapshot()["active"]:
        return "running"
    summaries = runtime.runs.dashboard_summary()
    statuses = {item["last_status"] for item in summaries if item["enabled"]}
    if "failed" in statuses:
        return "failed"
    if "partial" in statuses:
        return "partial"
    if "ok" in statuses:
        return "ok"
    return "paused"
