from pathlib import Path
import zipfile
import io

from fastapi.testclient import TestClient

from app.config import AppConfig, AppPaths
from app.main import create_app


def config(
    tmp_path: Path, *, user: str | None = None, password: str | None = None
) -> AppConfig:
    return AppConfig(
        host="127.0.0.1",
        port=8091,
        bind_lan=False,
        dashboard_user=user,
        dashboard_password=password,
        mode="dev",
        paths=AppPaths.from_root(tmp_path).ensure(),
    )


def test_dashboard_and_assets_are_local(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        page = client.get("/")
        script = client.get("/static/app.js")
        styles = client.get("/static/app.css")
        chart = client.get("/static/vendor/chart.umd.js")
    assert page.status_code == 200
    assert "FileHarvester" in page.text
    assert "/static/vendor/chart.umd.js" in page.text
    combined = page.text + script.text + styles.text
    assert "https://" not in combined
    assert "http://" not in combined
    assert script.status_code == styles.status_code == chart.status_code == 200
    assert len(chart.content) > 100_000


def test_optional_basic_auth_keeps_health_public(tmp_path: Path) -> None:
    app = create_app(config(tmp_path, user="operador", password="segura"))
    with TestClient(app) as client:
        health = client.get("/healthz")
        denied = client.get("/")
        allowed = client.get("/", auth=("operador", "segura"))
        api_denied = client.get("/api/connections")
        api_allowed = client.get(
            "/api/connections", auth=("operador", "segura")
        )
    assert health.status_code == 200
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Basic")
    assert allowed.status_code == 200
    assert api_denied.status_code == 401
    assert api_allowed.status_code == 200


def test_support_exports_and_alert_api(tmp_path: Path) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        alerts = client.get("/api/alerts")
        runs_csv = client.get("/api/export/runs.csv?days=7")
        files_csv = client.get("/api/export/files.csv")
        report = client.get("/api/export/report.html?days=7")
        bundle = client.get("/api/export/bundle.zip?days=7")
        missing_log = client.get("/api/runs/999/log.jsonl")
        retention = client.post("/api/retention/run")
    assert alerts.json() == {"items": []}
    assert runs_csv.text.startswith("\ufeffid,connection_id")
    assert files_csv.text.startswith("\ufeffid,run_id")
    assert "<title>Reporte FileHarvester" in report.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert "exports/configuration.json" in archive.namelist()
    assert missing_log.status_code == 404
    assert retention.json()["runs_deleted"] == 0
