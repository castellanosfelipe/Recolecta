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
    assert "Recolecta" in page.text
    assert "/static/vendor/chart.umd.js" in page.text
    assert 'src="http' not in page.text
    assert 'href="http' not in page.text.replace(
        'href="https://github.com/castellanosfelipe/'
        'Recolecta-Automatizaci-n-confiable-de-archivos"',
        "",
    ).replace(
        'href="https://www.linkedin.com/in/'
        'bairon-felipe-peña-castellanos-ab18411b5?'
        'utm_source=share_via&amp;utm_content=profile&amp;'
        'utm_medium=member_ios"',
        "",
    )
    assert "Abrir el repositorio de Recolecta en GitHub" in page.text
    assert "Bairon Felipe Peña Castellanos en LinkedIn" in page.text
    assert 'rel="noopener noreferrer"' in page.text
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


def test_connection_import_schedule_and_dialog_close_controls_are_exposed(
    tmp_path: Path,
) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text

    assert 'id="connection-import-button"' in page
    assert 'id="connection-import-file"' in page
    assert 'name="schedule_time" type="time"' in page
    for dialog_id in (
        "connection-dialog",
        "detail-dialog",
        "import-result-dialog",
    ):
        assert f'data-close-dialog="{dialog_id}"' in page
    assert (
        'type="button" data-close-dialog="connection-dialog"'
        in page
    )
    assert 'target.dataset.closeDialog' in script
    assert (
        "document.getElementById(target.dataset.closeDialog)?.close()"
        in script
    )
    assert '"/api/import/connections"' in script
    assert "La importación se completó, pero no se pudo actualizar la vista" in script


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
    assert "<title>Reporte Recolecta" in report.text
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert "exports/configuration.json" in archive.namelist()
    assert missing_log.status_code == 404
    assert retention.json()["runs_deleted"] == 0
