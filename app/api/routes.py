"""Minimal health and command routes used before the dashboard phase."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import __version__
from app.api.schemas import CancelResponse, RunNowRequest, RunNowResponse
from app.errors import HarvesterError
from app.orchestrator import RunCoordinator


def create_router(coordinator: RunCoordinator) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "app": "FileHarvester", "version": __version__}

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
        except HarvesterError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RunNowResponse(
            executions=[execution.summary() for execution in executions]
        )

    @router.post("/api/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(run_id: int) -> CancelResponse:
        return CancelResponse(
            run_id=run_id,
            cancelled=coordinator.cancel(run_id),
        )

    return router
