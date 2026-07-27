from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import create_router
from app.db import ConnectionRepository, Database, RunRepository
from app.models import Connection
from app.platform.secrets_fernet import FernetSecretStore
from app.progress import ProgressRegistry
from app.settings_store import SettingsStore


class Coordinator:
    def cancel(self, run_id: int) -> bool:
        return False


def api(tmp_path: Path) -> tuple[TestClient, ConnectionRepository, RunRepository]:
    database = Database(tmp_path / "harvester.db")
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    runs = RunRepository(database)
    app = FastAPI()
    app.include_router(
        create_router(
            Coordinator(),
            connections=connections,
            runs=runs,
            settings=SettingsStore(database),
            progress=ProgressRegistry(),
        )
    )
    return TestClient(app), connections, runs


def test_connection_crud_duplicate_and_secret_never_leaks(tmp_path: Path) -> None:
    client, repository, _ = api(tmp_path)
    payload = {
        "name": "Producción",
        "host": "sftp.example.test",
        "protocol": "SFTP",
        "remote_paths": ["/entrada"],
        "secret": "super-secreto",
    }
    with client:
        created = client.post("/api/connections", json=payload)
        listed = client.get("/api/connections")
        updated = client.patch(
            f"/api/connections/{created.json()['id']}",
            json={"notes": "auditada", "secret": "nuevo-secreto"},
        )
        duplicate = client.post(
            f"/api/connections/{created.json()['id']}/duplicate"
        )
        paused_run = client.post(
            f"/api/connections/{duplicate.json()['id']}/run"
        )
    assert created.status_code == 201
    assert created.json()["has_secret"] is True
    assert "secret" not in created.json()
    assert "secret_encrypted" not in created.json()
    assert "super-secreto" not in created.text
    assert "super-secreto" not in listed.text
    assert updated.json()["notes"] == "auditada"
    assert repository.get_secret(created.json()["id"]) == "nuevo-secreto"
    assert duplicate.status_code == 201
    assert duplicate.json()["enabled"] is False
    assert duplicate.json()["has_secret"] is False
    assert paused_run.status_code == 409


def test_history_files_dashboard_settings_and_csv(tmp_path: Path) -> None:
    client, connections, runs = api(tmp_path)
    saved = connections.create(
        Connection(name="Auditoría", host="example.test", remote_paths=("/in",))
    )
    started = datetime(2026, 7, 27, 3, tzinfo=timezone.utc)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
        started_at=started,
    )
    runs.finish_run(run_id, status="ok")
    with client:
        history = client.get("/api/runs?status=ok")
        detail = client.get(f"/api/runs/{run_id}")
        dashboard = client.get("/api/dashboard")
        settings = client.put(
            "/api/settings",
            json={
                "values": {
                    "concurrency.global": 3,
                    "schedule.daily_time": "03:15",
                    "schedule.jitter_s": 120,
                }
            },
        )
        exported = client.get("/api/files/export.csv")
    assert history.json()["items"][0]["connection_name"] == "Auditoría"
    assert detail.json()["files"] == []
    assert dashboard.json()["connections"][0]["last_status"] == "ok"
    assert settings.json()["values"]["concurrency.global"] == 3
    assert settings.json()["values"]["schedule.hour"] == 3
    assert settings.json()["values"]["schedule.minute"] == 15
    assert settings.json()["values"]["schedule.jitter_minutes"] == 2
    assert exported.status_code == 200
    assert exported.text.startswith("id,run_id,connection_name")
