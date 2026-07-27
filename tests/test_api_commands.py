from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import create_router
from app.errors import ErrorType, RecolectaError


class Execution:
    def __init__(self, connection_id: int, status: str = "ok") -> None:
        self.connection_id = connection_id
        self.status = status

    def summary(self):
        return {
            "connection_id": self.connection_id,
            "status": self.status,
        }


class Coordinator:
    def __init__(self) -> None:
        self.calls = []

    def execute_connection(self, connection_id, **kwargs):
        self.calls.append((connection_id, kwargs))
        return Execution(connection_id, "dry_run" if kwargs["dry_run_only"] else "ok")

    def execute_all(self, **kwargs):
        self.calls.append((None, kwargs))
        return (Execution(1), Execution(2))

    def cancel(self, run_id):
        return run_id == 7


def client(coordinator) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(coordinator))
    return TestClient(app)


def test_health_and_local_run_delegation() -> None:
    coordinator = Coordinator()
    with client(coordinator) as api:
        health = api.get("/healthz")
        response = api.post(
            "/api/commands/run-now",
            json={
                "connection_id": 3,
                "selected_date": "2026-07-26",
                "dry_run": True,
            },
        )
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert response.status_code == 200
    assert response.json()["executions"][0]["status"] == "dry_run"
    _, arguments = coordinator.calls[0]
    assert arguments["selected_date"] == date(2026, 7, 26)
    assert arguments["trigger"] == "manual"


def test_run_all_and_cancel_routes() -> None:
    coordinator = Coordinator()
    with client(coordinator) as api:
        response = api.post("/api/commands/run-now", json={})
        cancelled = api.post("/api/runs/7/cancel")
        missing = api.post("/api/runs/8/cancel")
    assert len(response.json()["executions"]) == 2
    assert cancelled.json() == {"run_id": 7, "cancelled": True}
    assert missing.json() == {"run_id": 8, "cancelled": False}


def test_active_connection_conflict_returns_409() -> None:
    class Busy(Coordinator):
        def execute_connection(self, connection_id, **kwargs):
            raise RecolectaError(
                ErrorType.INTERRUPTED, "Ya hay una corrida activa."
            )

    with client(Busy()) as api:
        response = api.post(
            "/api/commands/run-now", json={"connection_id": 1}
        )
    assert response.status_code == 409
    assert "corrida activa" in response.json()["detail"]
