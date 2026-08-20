from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes import _plan_response
from app.orchestrator import DryRunPlan, PlanItem, PlanStatus, TimeWindow
from app.statuses import (
    enrich_alert,
    enrich_file,
    enrich_plan,
    enrich_run,
    run_result_detail,
    run_result_label,
    run_result_status,
)
from app.transports.base import RemoteFile


@pytest.mark.parametrize(
    (
        "status",
        "files_found",
        "files_downloaded",
        "files_failed",
        "error_type",
        "expected",
    ),
    (
        ("ok", 0, 0, 0, None, "no_files"),
        ("ok", 4, 0, 0, None, "no_changes"),
        ("ok", 4, 3, 0, None, "completed"),
        ("partial", 0, 0, 0, None, "no_files"),
        ("partial", 4, 0, 0, None, "no_changes"),
        ("partial", 4, 3, 0, None, "completed"),
        ("partial", 4, 3, 1, None, "partial"),
        ("partial", 0, 0, 0, "protocol", "partial"),
        ("failed", 0, 0, 0, None, "failed"),
        ("running", 0, 0, 0, None, "running"),
        ("cancelled", 0, 0, 0, None, "cancelled"),
    ),
)
def test_run_result_status_recovers_warning_only_legacy_partials(
    status: str,
    files_found: int,
    files_downloaded: int,
    files_failed: int,
    error_type: str | None,
    expected: str,
) -> None:
    assert (
        run_result_status(
            status,
            files_found=files_found,
            files_downloaded=files_downloaded,
            files_failed=files_failed,
            error_type=error_type,
        )
        == expected
    )


def test_run_result_labels_distinguish_empty_success_from_missing_target() -> None:
    assert (
        run_result_label(
            "ok",
            files_found=0,
            files_downloaded=0,
        )
        == "Sin archivos encontrados"
    )
    assert (
        run_result_label(
            "failed",
            files_found=0,
            files_downloaded=0,
            error_type="target_missing",
        )
        == "Ruta remota no existente"
    )


def test_enrich_run_keeps_raw_status_and_does_not_mutate_input() -> None:
    source = {
        "id": 7,
        "status": "ok",
        "files_found": 2,
        "files_downloaded": 0,
        "error_type": None,
    }

    enriched = enrich_run(source)

    assert source == {
        "id": 7,
        "status": "ok",
        "files_found": 2,
        "files_downloaded": 0,
        "error_type": None,
    }
    assert enriched["status"] == "ok"
    assert enriched["result_status"] == "no_changes"
    assert enriched["status_label"] == "Sin archivos nuevos"
    assert enriched["discovery_scope"] is None


@pytest.mark.parametrize(
    ("files_found", "files_downloaded", "expected_status", "expected_label"),
    (
        (0, 0, "no_files", "Sin archivos encontrados"),
        (7, 0, "no_changes", "Sin archivos nuevos"),
        (7, 5, "completed", "Descarga completada"),
    ),
)
def test_enrich_run_preserves_raw_warning_only_partial_but_presents_success(
    files_found: int,
    files_downloaded: int,
    expected_status: str,
    expected_label: str,
) -> None:
    enriched = enrich_run(
        {
            "id": 8,
            "status": "partial",
            "files_found": files_found,
            "files_downloaded": files_downloaded,
            "files_failed": 0,
            "error_type": None,
        }
    )

    assert enriched["status"] == "partial"
    assert enriched["result_status"] == expected_status
    assert enriched["status_label"] == expected_label


def test_enrich_run_decodes_persisted_warnings_and_notices() -> None:
    source = {
        "status": "ok",
        "files_found": 0,
        "files_downloaded": 0,
        "warnings_json": '["ruta aislada"]',
        "notices_json": '["LIST compatible", "LIST compatible"]',
    }

    enriched = enrich_run(source)

    assert enriched["warnings"] == ["ruta aislada"]
    assert enriched["notices"] == ["LIST compatible"]
    assert "warnings_json" not in enriched
    assert "notices_json" not in enriched
    assert source["notices_json"].startswith("[")


@pytest.mark.parametrize(
    ("files_failed", "error_type"),
    (
        (1, None),
        (0, "protocol"),
    ),
)
def test_enrich_run_keeps_actionable_partial_visible(
    files_failed: int,
    error_type: str | None,
) -> None:
    enriched = enrich_run(
        {
            "status": "partial",
            "files_found": 2,
            "files_downloaded": 1,
            "files_failed": files_failed,
            "error_type": error_type,
        }
    )

    assert enriched["result_status"] == "partial"
    assert enriched["status_label"] == "Completada con incidencias"


@pytest.mark.parametrize(
    "evidence",
    (
        {"warnings_json": '["ruta aislada"]'},
        {"error_msg": "El listado remoto quedó incompleto."},
    ),
)
def test_enrich_run_does_not_hide_message_backed_partial(
    evidence: dict[str, object],
) -> None:
    enriched = enrich_run(
        {
            "status": "partial",
            "files_found": 0,
            "files_downloaded": 0,
            "files_failed": 0,
            "error_type": None,
            **evidence,
        }
    )

    assert enriched["result_status"] == "partial"
    assert enriched["status_label"] == "Completada con incidencias"


def test_enrich_run_exposes_historical_scope_and_explains_empty_root_only_scan(
) -> None:
    source = {
        "id": 9,
        "status": "ok",
        "files_found": 0,
        "files_downloaded": 0,
        "scan_mode": "window",
        "discovery_paths_json": '["/entrada"]',
        "discovery_recursive": 0,
        "discovery_max_depth": 3,
    }

    enriched = enrich_run(source)

    assert source["discovery_paths_json"] == '["/entrada"]'
    assert enriched["discovery_scope"] == {
        "remote_paths": ["/entrada"],
        "recursive": False,
        "max_depth": 3,
    }
    assert "discovery_paths_json" not in enriched
    assert "discovery_recursive" not in enriched
    assert "discovery_max_depth" not in enriched
    assert "directamente en la ruta configurada" in enriched["status_detail"]
    assert "Las subcarpetas no se exploraron" in enriched["status_detail"]


def test_empty_run_detail_distinguishes_depth_and_full_tree_scope() -> None:
    depth_limited = run_result_detail(
        "ok",
        files_found=0,
        discovery_scope={
            "remote_paths": ["/entrada", "/archivo"],
            "recursive": True,
            "max_depth": 2,
        },
        scan_mode="window",
    )
    full_tree = run_result_detail(
        "ok",
        files_found=0,
        discovery_scope={
            "remote_paths": ["/entrada"],
            "recursive": True,
            "max_depth": 2_147_483_647,
        },
        scan_mode="full_local_reconciliation",
    )

    assert "las 2 rutas configuradas" in depth_limited
    assert "hasta 2 nivel(es)" in depth_limited
    assert "árbol remoto de la ruta configurada" in full_tree


@pytest.mark.parametrize(
    ("enricher", "source", "expected_label"),
    (
        (
            enrich_file,
            {"status": "ok", "error_type": None},
            "Descargado y verificado",
        ),
        (
            enrich_file,
            {"status": "failed", "error_type": "integrity"},
            "Validación del archivo fallida",
        ),
        (
            enrich_alert,
            {"status": "sent"},
            "Enviada",
        ),
        (
            enrich_alert,
            {"status": "pending"},
            "Pendiente de envío",
        ),
    ),
)
def test_domain_enrichers_use_context_specific_labels(
    enricher,
    source: dict[str, object],
    expected_label: str,
) -> None:
    enriched = enricher(source)

    assert enriched is not source
    assert enriched["status"] == source["status"]
    assert enriched["status_label"] == expected_label


def test_enrich_plan_labels_items_and_summarizes_an_empty_listing() -> None:
    empty = enrich_plan(
        {
            "files_to_download": 0,
            "items": [],
        }
    )
    planned = enrich_plan(
        {
            "files_to_download": 0,
            "items": [
                {"status": "duplicate", "remote_path": "/in/repetido.csv"},
                {"status": "outside_window", "remote_path": "/in/antiguo.csv"},
            ],
        }
    )

    assert empty["result_status"] == "no_files"
    assert empty["result_label"] == "Sin archivos encontrados"
    assert planned["result_status"] == "no_changes"
    assert planned["result_label"] == "Sin archivos nuevos"
    assert [item["status_label"] for item in planned["items"]] == [
        "Ya descargado",
        "Fuera del período configurado",
    ]


def test_enrich_plan_uses_explicit_total_when_items_are_a_sample() -> None:
    enriched = enrich_plan(
        {
            "files_found": 1_000_000,
            "files_to_download": 0,
            "items_truncated": True,
            "items": [],
        }
    )

    assert enriched["files_found"] == 1_000_000
    assert enriched["result_status"] == "no_changes"
    assert enriched["result_label"] == "Sin archivos nuevos"


def test_enrich_plan_prioritizes_actionable_incidents_over_empty_counts() -> None:
    enriched = enrich_plan(
        {
            "files_found": 1,
            "files_to_download": 0,
            "is_partial": True,
            "warnings": ["Una ruta remota no se puede materializar."],
            "items": [
                {"status": "path_invalid", "remote_path": "/in/../escape"}
            ],
        }
    )

    assert enriched["result_status"] == "partial"
    assert enriched["result_label"] == "Simulación con incidencias"


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        ("local_present", "Ya existe y coincide en destino"),
        ("local_missing", "No existe en destino"),
        ("local_different", "Existe, pero no coincide"),
        ("path_invalid", "Ruta remota no permitida"),
    ),
)
def test_reconciliation_plan_statuses_have_descriptive_labels(
    status: str,
    expected: str,
) -> None:
    enriched = enrich_plan(
        {
            "files_found": 1,
            "files_to_download": int(status != "local_present"),
            "items": [{"status": status, "remote_path": "/in/documento.bin"}],
        }
    )

    assert enriched["items"][0]["status_label"] == expected


def test_plan_response_exposes_full_totals_and_bounded_sample_metadata() -> None:
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    plan = DryRunPlan(
        connection_id=8,
        window=TimeWindow(now - timedelta(days=1), now),
        items=(
            PlanItem(
                RemoteFile("/in/documento.bin", 12, now),
                PlanStatus.LOCAL_MISSING,
            ),
        ),
        total_items=1_000_000,
        planned_total=900_000,
        planned_bytes=12_345_678,
        scan_mode="full_local_reconciliation",
    )

    response = _plan_response(
        plan,
        discovery_scope={
            "remote_paths": ["/entrada"],
            "recursive": True,
            "max_depth": 2_147_483_647,
        },
    )

    assert response["files_found"] == 1_000_000
    assert response["files_to_download"] == 900_000
    assert response["discovery_scope"] == {
        "remote_paths": ["/entrada"],
        "recursive": True,
        "max_depth": 2_147_483_647,
    }
    assert response["planned_bytes"] == 12_345_678
    assert response["scan_mode"] == "full_local_reconciliation"
    assert response["items_truncated"] is True
    assert len(response["items"]) == 1
