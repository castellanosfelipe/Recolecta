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
        'href="https://github.com/castellanosfelipe/Recolecta"',
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
        styles = client.get("/static/app.css").text

    assert 'id="connection-import-button"' in page
    assert 'id="connection-import-file"' in page
    assert 'name="schedule_time" type="time"' in page
    assert 'name="remote_paths" rows="2" required' in page
    assert 'name="recursive" type="checkbox"' in page
    assert 'name="max_depth" type="number" min="0" value="3"' in page
    assert 'name="full_local_reconciliation" type="checkbox"' in page
    assert "Comparación completa" in page
    assert "repara archivos ausentes o diferentes" in page
    assert "conserva los extras locales" in page
    assert "Puede tardar en orígenes grandes." in page
    assert 'id="connection-test-button" type="button"' in page
    assert (
        'id="connection-save-button" type="submit" '
        'value="default" aria-describedby="connection-validation-status" '
        "disabled"
    ) in page
    assert 'id="connection-validation-status"' in page
    assert 'role="status" aria-live="polite"' in page
    assert 'id="connection-form" method="dialog" aria-busy="false"' in page
    assert (
        'id="connection-test-button" type="button" '
        'aria-describedby="connection-validation-status"'
    ) in page
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
    assert "/api/connections/validate" in script
    assert "testConnectionDraft" in script
    assert "invalidateConnectionValidation" in script
    assert "connectionValidatedRevision" in script
    assert "connectionValidatedFingerprint" in script
    assert "connectionPayloadFingerprint" in script
    assert "setConnectionDialogBusy" in script
    assert "state.connectionSaving" in script
    assert 'addEventListener("cancel"' in script
    assert "event.preventDefault()" in script
    assert '"Guardando y revalidando la conexión y sus rutas…"' in script
    assert 'addEventListener("input"' in script
    assert 'addEventListener("change"' in script
    assert (
        "Debes probar correctamente la conexión y sus rutas antes de guardar."
        in script
    )
    assert "La importación se completó, pero no se pudo actualizar la vista" in script
    assert 'data-full-local-reconciliation="${item.id}"' in script
    assert "Comparar todo el árbol" in script
    assert "Repara ausentes o diferentes; conserva extras locales" in script
    assert "no elimina archivos locales extra" in script
    assert "toggleFullLocalReconciliation" in script
    assert 'body: { full_local_reconciliation: enabled }' in script
    assert "input.checked = previous" in script
    assert "Comparación completa habilitada." in script
    assert "payload.recursive = form.elements.recursive.checked" in script
    assert "payload.full_local_reconciliation" in script
    assert ".reconciliation-action" in styles
    assert ".check-with-help" in styles


def test_dashboard_exposes_descriptive_domain_specific_statuses(
    tmp_path: Path,
) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text
        styles = client.get("/static/app.css").text

    for value, label in (
        ("no_files", "Archivos no existentes"),
        ("no_changes", "Sin archivos nuevos"),
        ("completed", "Descarga completada"),
        ("partial", "Completada con incidencias"),
        ("failed", "Ejecución fallida"),
        ("cancelled", "Cancelada por el usuario"),
    ):
        assert f'<option value="{value}">{label}</option>' in page
        assert f'{value}: "{label}"' in script

    for value, label in (
        ("pending", "Pendiente de descarga"),
        ("ok", "Descargado y verificado"),
        ("skipped", "Omitido por configuración"),
        ("duplicate", "Ya descargado"),
        ("failed", "No se pudo descargar"),
    ):
        assert f'<option value="{value}">{label}</option>' in page
        assert f'{value}: "{label}"' in script

    assert 'sent: "Enviada"' in script
    assert 'failed: "No enviada"' in script
    assert "runStatusBadge(run)" in script
    assert "fileStatusBadge(file)" in script
    assert "alertStatusBadge(alert)" in script
    assert ".status.no_files" in styles
    assert ".status.no_changes" in styles


def test_dashboard_exposes_responsive_and_accessible_interactions(
    tmp_path: Path,
) -> None:
    app = create_app(config(tmp_path))
    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/static/app.js").text
        styles = client.get("/static/app.css").text

    assert 'aria-current="page"' in page
    assert 'id="runs-chart-summary" class="sr-only"' in page
    assert 'aria-describedby="runs-chart-summary"' in page
    for caption in (
        "Historial de corridas",
        "Archivos procesados",
        "Conexiones configuradas",
        "Registro de alertas recientes",
    ):
        assert f'<caption class="sr-only">{caption}</caption>' in page

    assert 'id="simulation-dialog"' in page
    assert 'data-close-dialog="simulation-dialog"' in page
    assert "showSimulationResult(result)" in script
    assert "const pendingToast = toast" in script
    assert "pendingToast.remove()" in script
    assert 'event.key !== "Escape"' in script
    assert '$$("dialog[open]")' in script
    assert "Contadores del plan" in script
    assert "Muestra del plan" in script
    assert "result.items_truncated" in script
    assert "result.warnings" in script
    assert '<caption>Muestra de ${items.length}' in script

    assert 'name="ssl_mode"' in page
    assert '<option value="required" selected>' in page
    assert '<option value="insecure">' in page
    assert 'protocol === "FTPS" || protocol === "WEBDAVS"' in script
    assert "field.hidden = !usesTls" in script
    assert "select.disabled = !usesTls" in script
    assert 'payload.ssl_mode = form.elements.namedItem("ssl_mode").value' in script
    assert "updateTlsModeField()" in script
    assert '[hidden] { display: none !important; }' in styles
    assert 'id="ssl-mode-field" hidden' in page
    assert 'aria-describedby="dest-root-help"' in page
    assert 'id="dest-root-help"' in page
    assert "En modo servicio/SYSTEM, usa una ruta UNC" in page
    assert "las unidades mapeadas, como W:" in page
    assert "Tope agregado de ancho de banda" in page

    assert 'setAttribute("aria-current", "page")' in script
    assert 'removeAttribute("aria-current")' in script
    assert 'setView(location.hash.slice(1) || "inicio", { focusMain: false })' in script
    assert "dialog.scrollTop = 0" in script
    assert 'namedItem("name").focus({ preventScroll: true })' in script

    assert 'error ? "alert" : "status"' in script
    assert 'error ? "assertive" : "polite"' in script
    assert "toast-close" in script
    assert 'pause("pointer")' in script
    assert 'pause("focus")' in script
    assert "error ? 12000 : 7000" in script
    assert 'aria-valuetext="Progreso indeterminado;' in script
    assert 'aria-valuenow="${file.percent ?? 0}"' not in script

    assert "grid-template-columns: 238px minmax(0, 1fr)" in styles
    assert ".chart-wrap canvas" in styles
    assert "max-width: 100% !important" in styles
    assert ".table-panel { width: 100%; max-width: 100%; min-width: 0;" in styles
    assert ".progress-track > span.indeterminate" in styles
    assert ".toast-close" in styles


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
