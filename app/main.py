"""Resident application lifecycle: database, scheduler, and local API."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from app.api.routes import create_router
from app.config import AppConfig
from app.db import ConnectionRepository, Database, RunRepository
from app.logging_setup import configure_logging
from app.orchestrator import RunCoordinator
from app.platform.secretstore import create_secret_store
from app.platform.single_instance import SingleInstance
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
    coordinator = RunCoordinator(
        database,
        connections,
        config.paths,
        throttle=throttle,
    )
    scheduler = SchedulerService(coordinator, connections, runs)
    scheduler.configure(SchedulerSettings.load(settings))
    return RuntimeComponents(
        database,
        connections,
        runs,
        settings,
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
    app.include_router(create_router(components.coordinator))
    return app


def run_resident(config: AppConfig | None = None) -> int:
    resolved = config or AppConfig.from_env()
    configure_logging(resolved.paths.logs)
    guard = SingleInstance(resolved.paths.data)
    if not guard.try_acquire():
        logger.error("FileHarvester ya se está ejecutando.")
        return 2
    try:
        app = create_app(resolved)
        uvicorn.run(
            app,
            host=resolved.host,
            port=resolved.port,
            log_config=None,
            access_log=False,
        )
        return 0
    finally:
        guard.release()
