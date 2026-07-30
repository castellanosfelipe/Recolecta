from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes import _plan_response
from app.orchestrator import DryRunPlan, PlanItem, PlanStatus, TimeWindow
from app.statuses import (
    enrich_alert,
    enrich_file,
    enrich_plan,
    enrich_run,
    run_result_label,
    run_result_status,
)
from app.transports.base import RemoteFile


@pytest.mark.parametrize(
    ("status", "files_found", "files_downloaded", "expected"),
    (
        ("ok", 0, 0, "no_files"),
        ("ok", 4, 0, "no_changes"),
        ("ok", 4, 3, "completed"),
        ("partial", 0, 0, "partial"),
        ("failed", 0, 0, "failed"),
        ("running", 0, 0, "running"),
        ("cancelled", 0, 0, "cancelled"),
    ),
)
def test_run_result_status_preserves_canonical_failures(
    status: str,
    files_found: int,
    files_downloaded: int,
    expected: str,
) -> None:
    assert (
        run_result_status(
            status,
            files_found=files_found,
            files_downloaded=files_downloaded,
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
        == "Archivos no existentes"
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
    assert empty["result_label"] == "Archivos no existentes"
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

    response = _plan_response(plan)

    assert response["files_found"] == 1_000_000
    assert response["files_to_download"] == 900_000
    assert response["planned_bytes"] == 12_345_678
    assert response["scan_mode"] == "full_local_reconciliation"
    assert response["items_truncated"] is True
    assert len(response["items"]) == 1
