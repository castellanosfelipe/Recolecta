from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import create_router
from app.connection_validation import ConnectionValidationResult
from app.db import ConnectionRepository, Database, RunRepository
from app.errors import ErrorType, RecolectaError
from app.models import Connection
from app.platform.secrets_fernet import FernetSecretStore
from app.progress import ProgressRegistry
from app.settings_store import SettingsStore
from app.transports.base import RemoteFile


class Coordinator:
    def __init__(self) -> None:
        self.validations: list[tuple[Connection, str | None]] = []
        self.validation_error: Exception | None = None
        self.submissions: list[tuple[int, dict[str, object]]] = []
        self.submission_error: Exception | None = None
        self.connections: ConnectionRepository | None = None

    def cancel(self, run_id: int) -> bool:
        return False

    def validate_connection_draft(
        self,
        connection: Connection,
        *,
        secret: str | None,
    ) -> ConnectionValidationResult:
        self.validations.append((connection, secret))
        if self.validation_error is not None:
            raise self.validation_error
        return ConnectionValidationResult(
            local_path=connection.dest_root,
            remote_paths=connection.remote_paths,
            remote_files_found=0,
            warnings=(),
        )

    def submit_connection(self, connection_id: int, **kwargs) -> None:
        if self.submission_error is not None:
            raise self.submission_error
        self.submissions.append((connection_id, kwargs))

    def delete_connection(self, connection_id: int) -> bool:
        assert self.connections is not None
        return self.connections.delete_if_idle(connection_id)


def api(
    tmp_path: Path,
    *,
    coordinator: Coordinator | None = None,
) -> tuple[TestClient, ConnectionRepository, RunRepository]:
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    runs = RunRepository(database)
    selected_coordinator = coordinator or Coordinator()
    selected_coordinator.connections = connections
    app = FastAPI()
    app.include_router(
        create_router(
            selected_coordinator,
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
        "schedule_time": "05:35",
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
    assert created.json()["schedule_time"] == "05:35"
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


def test_draft_validation_uses_new_or_stored_secret_without_persisting(
    tmp_path: Path,
) -> None:
    coordinator = Coordinator()
    client, repository, _ = api(tmp_path, coordinator=coordinator)
    payload = {
        "name": "Nueva",
        "host": "ftp.example.test",
        "protocol": "FTP",
        "remote_paths": ["/entrada"],
        "dest_root": str(tmp_path / "destino"),
        "secret": "secreto-nuevo",
    }

    with client:
        new_result = client.post("/api/connections/validate", json=payload)
        saved = repository.create(
            Connection(
                name="Guardada",
                host="sftp.example.test",
                remote_paths=("/original",),
                dest_root=str(tmp_path / "guardada"),
            ),
            secret="secreto-guardado",
        )
        edit_result = client.post(
            f"/api/connections/validate?connection_id={saved.id}",
            json={"remote_paths": ["/editada"]},
        )

    assert new_result.status_code == 200
    assert new_result.json()["valid"] is True
    assert coordinator.validations[0][1] == "secreto-nuevo"
    assert edit_result.status_code == 200
    assert coordinator.validations[1][0].remote_paths == ("/editada",)
    assert coordinator.validations[1][1] == "secreto-guardado"
    persisted = repository.get(saved.id)
    assert persisted.remote_paths == ("/original",)
    assert repository.get_secret(saved.id) == "secreto-guardado"


def test_failed_validation_blocks_create_and_connectivity_update(
    tmp_path: Path,
) -> None:
    coordinator = Coordinator()
    client, repository, _ = api(tmp_path, coordinator=coordinator)
    existing = repository.create(
        Connection(
            name="Existente",
            host="old.example.test",
            remote_paths=("/entrada",),
        ),
        secret="no-filtrar",
    )
    coordinator.validation_error = RecolectaError(
        ErrorType.AUTH,
        "La credencial fue rechazada por el servidor remoto.",
    )

    with client:
        rejected_create = client.post(
            "/api/connections",
            json={
                "name": "Rechazada",
                "host": "new.example.test",
                "protocol": "FTP",
                "remote_paths": ["/entrada"],
                "secret": "no-filtrar",
            },
        )
        rejected_update = client.patch(
            f"/api/connections/{existing.id}",
            json={"host": "changed.example.test", "secret": "no-filtrar"},
        )

    assert rejected_create.status_code == 422
    assert rejected_update.status_code == 422
    assert "credencial fue rechazada" in rejected_create.text.lower()
    assert "no-filtrar" not in rejected_create.text
    assert "no-filtrar" not in rejected_update.text
    assert [item.name for item in repository.list()] == ["Existente"]
    assert repository.get(existing.id).host == "old.example.test"
    assert repository.get_secret(existing.id) == "no-filtrar"


def test_changing_credential_scope_requires_an_explicit_secret(
    tmp_path: Path,
) -> None:
    coordinator = Coordinator()
    client, repository, _ = api(tmp_path, coordinator=coordinator)
    existing = repository.create(
        Connection(
            name="Protegida",
            protocol="FTP",
            host="original.example.test",
            username="reader",
            remote_paths=("/entrada",),
        ),
        secret="credencial-guardada",
    )

    with client:
        rejected_test = client.post(
            f"/api/connections/validate?connection_id={existing.id}",
            json={"host": "otro.example.test"},
        )
        rejected_save = client.patch(
            f"/api/connections/{existing.id}",
            json={"host": "otro.example.test"},
        )
        accepted_test = client.post(
            f"/api/connections/validate?connection_id={existing.id}",
            json={
                "host": "otro.example.test",
                "secret": "credencial-nueva",
            },
        )

    assert rejected_test.status_code == 422
    assert rejected_save.status_code == 422
    assert "vuelve a ingresar la credencial" in rejected_test.text.lower()
    assert "credencial-guardada" not in rejected_test.text
    assert len(coordinator.validations) == 1
    assert coordinator.validations[0][1] == "credencial-nueva"
    assert accepted_test.status_code == 200
    assert repository.get(existing.id).host == "original.example.test"
    assert repository.get_secret(existing.id) == "credencial-guardada"


def test_stability_backup_import_endpoint_reports_each_result(
    tmp_path: Path,
) -> None:
    client, repository, _ = api(tmp_path)
    backup = {
        "app": "StabilityMonitor",
        "version": "2.0.0",
        "connections": [
            {
                "name": "Entrada FTP",
                "protocol": "FTP",
                "host": "ftp.example.test",
                "port": 21,
                "username": "reader",
                "targets_json": '["/entrada"]',
                "enabled": 1,
            },
            {
                "name": "Base Oracle",
                "protocol": "ORACLE",
                "host": "db.example.test",
                "port": 1521,
            },
        ],
    }

    with client:
        imported = client.post("/api/import/connections", json=backup)

    assert imported.status_code == 201
    assert imported.json()["total"] == 2
    assert imported.json()["created_count"] == 1
    assert imported.json()["skipped_count"] == 1
    assert imported.json()["error_count"] == 0
    assert imported.json()["skipped"][0]["protocol"] == "ORACLE"
    saved = repository.list()[0]
    assert saved.name == "Entrada FTP"
    assert saved.remote_paths == ("/entrada",)
    assert saved.enabled is False


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
        rejected_secret = client.put(
            "/api/settings",
            json={"values": {"alerts.smtp.password": "no-guardar"}},
        )
    assert history.json()["items"][0]["connection_name"] == "Auditoría"
    assert history.json()["items"][0]["status"] == "ok"
    assert history.json()["items"][0]["result_status"] == "no_files"
    assert history.json()["items"][0]["status_label"] == (
        "Archivos no existentes"
    )
    assert detail.json()["status"] == "ok"
    assert detail.json()["result_status"] == "no_files"
    assert detail.json()["status_label"] == "Archivos no existentes"
    assert detail.json()["files"] == []
    assert detail.json()["files_returned"] == 0
    assert detail.json()["files_truncated"] is False
    assert dashboard.json()["connections"][0]["last_status"] == "ok"
    assert dashboard.json()["connections"][0]["last_result_status"] == (
        "no_files"
    )
    assert dashboard.json()["connections"][0]["last_status_label"] == (
        "Archivos no existentes"
    )
    assert settings.json()["values"]["concurrency.global"] == 3
    assert settings.json()["values"]["schedule.hour"] == 3
    assert settings.json()["values"]["schedule.minute"] == 15
    assert settings.json()["values"]["schedule.jitter_minutes"] == 2
    assert exported.status_code == 200
    assert exported.text.startswith("id,run_id,connection_name")
    assert rejected_secret.status_code == 422
    assert "no-guardar" not in rejected_secret.text


def test_run_detail_returns_at_most_500_files_and_reports_truncation(
    tmp_path: Path,
) -> None:
    client, connections, runs = api(tmp_path)
    saved = connections.create(
        Connection(
            name="Cola masiva",
            host="example.test",
            remote_paths=("/in",),
        )
    )
    started = datetime(2026, 7, 27, 3, tzinfo=timezone.utc)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
        started_at=started,
    )
    for index in range(501):
        runs.add_file(
            run_id=run_id,
            connection_id=saved.id,
            remote_file=RemoteFile(
                f"/in/documento-{index:04d}.bin",
                index,
                started,
            ),
            status="duplicate",
        )
    runs.finish_run(run_id, status="ok")

    with client:
        detail = client.get(f"/api/runs/{run_id}")
        second_page = client.get(
            f"/api/files?run_id={run_id}&limit=1&offset=500"
        )

    payload = detail.json()
    assert payload["files_found"] == 501
    assert payload["files_returned"] == 500
    assert payload["files_truncated"] is True
    assert len(payload["files"]) == 500
    assert len(second_page.json()["items"]) == 1


def test_run_filters_use_visual_results_without_hiding_canonical_status(
    tmp_path: Path,
) -> None:
    client, connections, runs = api(tmp_path)
    saved = connections.create(
        Connection(
            name="Estados",
            host="example.test",
            remote_paths=("/in",),
        )
    )
    started = datetime(2026, 7, 27, 3, tzinfo=timezone.utc)

    empty_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
        started_at=started,
    )
    runs.finish_run(empty_id, status="ok")

    unchanged_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started,
        window_end_utc=started + timedelta(hours=1),
        started_at=started + timedelta(hours=1),
    )
    runs.add_file(
        run_id=unchanged_id,
        connection_id=saved.id,
        remote_file=RemoteFile(
            "/in/ya-descargado.csv",
            12,
            started,
        ),
        status="duplicate",
    )
    runs.finish_run(unchanged_id, status="ok")

    completed_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started + timedelta(hours=1),
        window_end_utc=started + timedelta(hours=2),
        started_at=started + timedelta(hours=2),
    )
    runs.add_file(
        run_id=completed_id,
        connection_id=saved.id,
        remote_file=RemoteFile(
            "/in/nuevo.csv",
            20,
            started + timedelta(hours=1),
        ),
        status="ok",
    )
    runs.finish_run(completed_id, status="ok")

    failed_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started + timedelta(hours=2),
        window_end_utc=started + timedelta(hours=3),
        started_at=started + timedelta(hours=3),
    )
    runs.finish_run(
        failed_id,
        status="failed",
        error_type="target_missing",
        error_msg="No existe /in",
    )

    with client:
        empty = client.get("/api/runs?status=no_files")
        unchanged = client.get("/api/runs?status=no_changes")
        completed = client.get("/api/runs?status=completed")
        failed = client.get(f"/api/runs/{failed_id}")
        completed_detail = client.get(f"/api/runs/{completed_id}")

    assert [item["id"] for item in empty.json()["items"]] == [empty_id]
    assert empty.json()["items"][0]["status"] == "ok"
    assert [item["id"] for item in unchanged.json()["items"]] == [
        unchanged_id
    ]
    assert unchanged.json()["items"][0]["status_label"] == (
        "Sin archivos nuevos"
    )
    assert [item["id"] for item in completed.json()["items"]] == [
        completed_id
    ]
    assert completed.json()["items"][0]["status_label"] == (
        "Descarga completada"
    )
    assert completed_detail.json()["files"][0]["status_label"] == (
        "Descargado y verificado"
    )
    assert failed.json()["status"] == "failed"
    assert failed.json()["result_status"] == "failed"
    assert failed.json()["status_label"] == "Ruta remota no existente"
    assert failed.json()["status_label"] != "Archivos no existentes"


def test_connection_error_paths_progress_and_delete(tmp_path: Path) -> None:
    client, _, _ = api(tmp_path)
    payload = {
        "name": "Temporal",
        "host": "sftp.example.test",
        "protocol": "SFTP",
        "remote_paths": ["/entrada"],
    }
    with client:
        created = client.post("/api/connections", json=payload)
        connection_id = created.json()["id"]
        progress = client.get("/api/progress")
        current = client.get("/api/runs/current")
        missing_get = client.get("/api/connections/9999")
        missing_patch = client.patch(
            "/api/connections/9999", json={"notes": "x"}
        )
        missing_duplicate = client.post("/api/connections/9999/duplicate")
        updated = client.patch(
            f"/api/connections/{connection_id}",
            json={"notes": "sin cambiar secreto"},
        )
        deleted = client.delete(f"/api/connections/{connection_id}")
        missing_delete = client.delete(f"/api/connections/{connection_id}")

    assert progress.status_code == 200
    assert progress.json() == current.json()
    assert updated.json()["notes"] == "sin cambiar secreto"
    assert deleted.status_code == 204
    for response in (
        missing_get,
        missing_patch,
        missing_duplicate,
        missing_delete,
    ):
        assert response.status_code == 404


def test_active_run_blocks_connection_delete_and_preserves_audit_rows(
    tmp_path: Path,
) -> None:
    client, connections, runs = api(tmp_path)
    saved = connections.create(
        Connection(
            name="No eliminar",
            host="sftp.example.test",
            remote_paths=("/entrada",),
        )
    )
    started = datetime(2026, 7, 27, 3, tzinfo=timezone.utc)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=started - timedelta(days=1),
        window_end_utc=started,
        started_at=started,
    )
    runs.add_file(
        run_id=run_id,
        connection_id=saved.id,
        remote_file=RemoteFile("/entrada/pendiente.bin", 10, started),
    )

    with client:
        rejected = client.delete(f"/api/connections/{saved.id}")

    assert rejected.status_code == 409
    assert "corrida activa" in rejected.json()["detail"]
    assert connections.get(saved.id).id == saved.id
    with connections.database.connect() as database:
        assert database.execute(
            "SELECT COUNT(*) FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0] == 1
        assert database.execute(
            "SELECT COUNT(*) FROM run_files WHERE run_id = ?", (run_id,)
        ).fetchone()[0] == 1


def test_run_endpoint_rejects_active_connection_before_returning_accepted(
    tmp_path: Path,
) -> None:
    coordinator = Coordinator()
    client, connections, _ = api(tmp_path, coordinator=coordinator)
    saved = connections.create(
        Connection(
            name="Ocupada",
            host="sftp.example.test",
            remote_paths=("/entrada",),
        )
    )
    coordinator.submission_error = RecolectaError(
        ErrorType.INTERRUPTED,
        "Ya hay una corrida activa para Ocupada.",
    )

    with client:
        rejected = client.post(f"/api/connections/{saved.id}/run")

    assert rejected.status_code == 409
    assert "corrida activa" in rejected.json()["detail"]
    assert "accepted" not in rejected.json()
    assert coordinator.submissions == []


def test_settings_validation_reports_actionable_errors(tmp_path: Path) -> None:
    client, _, _ = api(tmp_path)
    payloads = (
        ({"schedule.daily_time": "not-a-time"}, "formato HH:MM"),
        ({"schedule.jitter_s": "later"}, "número entero"),
        ({"schedule.jitter_s": -1}, "no puede ser negativo"),
        ({"concurrency.global": 0}, "al menos uno"),
        ({"bandwidth.global_kbps": -1}, "no puede ser negativo"),
        ({"disk.reserve_percent": 101}, "entre 0 y 100"),
        ({"courtesy.minimum_spacing_s": -1}, "no puede ser negativo"),
        ({"alerts.partial_threshold": 0}, "al menos uno"),
        ({"alerts.smtp.port": 70000}, "entre 1 y 65535"),
    )
    with client:
        responses = [
            (
                client.put("/api/settings", json={"values": values}),
                expected,
            )
            for values, expected in payloads
        ]

    for response, expected in responses:
        assert response.status_code == 422
        assert expected in response.json()["detail"]
