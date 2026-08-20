"""Presentation labels derived from stable persisted status codes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


RUN_RESULT_LABELS = {
    "completed": "Descarga completada",
    "no_files": "Sin archivos encontrados",
    "no_changes": "Sin archivos nuevos",
    "ok": "Ejecución sin errores",
    "partial": "Completada con incidencias",
    "failed": "Ejecución fallida",
    "running": "En ejecución",
    "cancelled": "Cancelada por el usuario",
}

RUN_ERROR_LABELS = {
    "auth": "Credencial rechazada",
    "dns": "Servidor no encontrado",
    "tcp_connect": "Servidor no disponible",
    "tcp_timeout": "Tiempo de conexión agotado",
    "tls": "Seguridad TLS/SSH no validada",
    "permission": "Acceso denegado",
    "target_missing": "Ruta remota no existente",
    "protocol": "Error de protocolo",
    "disk_space": "Espacio local insuficiente",
    "disk_write": "No se pudo escribir en el destino",
    "integrity": "Validación del archivo fallida",
    "partial_transfer": "Transferencia incompleta",
    "path_invalid": "Ruta no permitida",
    "interrupted": "Ejecución interrumpida",
    "timestamp_unreliable": "Fecha remota no confiable",
    "unknown": "Error no identificado",
}

FILE_STATUS_LABELS = {
    "pending": "Pendiente de descarga",
    "downloading": "Descargando",
    "ok": "Descargado y verificado",
    "skipped": "Omitido por configuración",
    "duplicate": "Ya descargado",
    "failed": "No se pudo descargar",
    "cancelled": "Descarga cancelada",
}

PLAN_STATUS_LABELS = {
    "planned": "Listo para descargar",
    "local_present": "Ya existe y coincide en destino",
    "local_missing": "No existe en destino",
    "local_different": "Existe, pero no coincide",
    "path_invalid": "Ruta remota no permitida",
    "duplicate": "Ya descargado",
    "outside_window": "Fuera del período configurado",
    "quiet_period": "Todavía en escritura",
    "timestamp_missing": "Sin fecha remota",
    "include_filter": "No coincide con la inclusión",
    "exclude_filter": "Excluido por configuración",
    "size_filter": "Fuera del tamaño permitido",
    "symlink": "Enlace simbólico omitido",
}

ALERT_STATUS_LABELS = {
    "sent": "Enviada",
    "pending": "Pendiente de envío",
    "failed": "No enviada",
}

CONNECTION_STATUS_LABELS = {
    "enabled": "Activa y programada",
    "disabled": "En pausa",
    "never_run": "Sin ejecuciones",
}

PLAN_RESULT_LABELS = {
    "partial": "Simulación con incidencias",
    "no_files": "Sin archivos encontrados",
    "no_changes": "Sin archivos nuevos",
    "files_ready": "Archivos listos para descargar",
}

SUCCESSFUL_RUN_RESULTS = frozenset(
    {"completed", "no_files", "no_changes", "ok"}
)


def run_result_status(
    status: object,
    *,
    files_found: object = 0,
    files_downloaded: object = 0,
    files_failed: object = 0,
    error_type: object = None,
    error_msg: object = None,
    warnings: object = None,
) -> str:
    """Derive a descriptive result without changing the canonical run status."""
    canonical = str(status or "")
    if _is_legacy_warning_only_partial(
        canonical,
        files_failed=files_failed,
        error_type=error_type,
        error_msg=error_msg,
        warnings=warnings,
    ):
        # Older releases persisted non-fatal transport notices (notably FTP
        # LIST timestamp precision) as ``partial``.  Preserve the raw value
        # for audit compatibility while presenting the completed outcome.
        canonical = "ok"
    if canonical != "ok":
        return canonical or "unknown"
    if _as_int(files_found) == 0:
        return "no_files"
    if _as_int(files_downloaded) == 0:
        return "no_changes"
    return "completed"


def run_result_label(
    status: object,
    *,
    files_found: object = 0,
    files_downloaded: object = 0,
    files_failed: object = 0,
    error_type: object = None,
    error_msg: object = None,
    warnings: object = None,
) -> str:
    """Return the operator-facing label for one persisted run."""
    canonical = str(status or "")
    error_code = str(error_type or "")
    if canonical == "failed" and error_code:
        return RUN_ERROR_LABELS.get(error_code, RUN_RESULT_LABELS["failed"])
    result = run_result_status(
        canonical,
        files_found=files_found,
        files_downloaded=files_downloaded,
        files_failed=files_failed,
        error_type=error_type,
        error_msg=error_msg,
        warnings=warnings,
    )
    return RUN_RESULT_LABELS.get(result, "Estado no identificado")


def run_result_detail(
    status: object,
    *,
    files_found: object = 0,
    files_downloaded: object = 0,
    files_failed: object = 0,
    error_type: object = None,
    error_msg: object = None,
    warnings: object = None,
    discovery_scope: Mapping[str, Any] | None = None,
    scan_mode: object = None,
) -> str:
    """Explain what a run result means in plain Spanish."""
    result = run_result_status(
        status,
        files_found=files_found,
        files_downloaded=files_downloaded,
        files_failed=files_failed,
        error_type=error_type,
        error_msg=error_msg,
        warnings=warnings,
    )
    if result == "no_files":
        if discovery_scope is not None:
            return _empty_discovery_detail(
                discovery_scope,
                scan_mode=scan_mode,
            )
        return (
            "La conexión respondió correctamente, pero no se encontraron "
            "archivos dentro del alcance configurado."
        )
    if result == "no_changes":
        return (
            "Se encontraron archivos, pero ninguno requería una nueva descarga."
        )
    if result == "completed":
        count = _as_int(files_downloaded)
        return f"Se descargaron y verificaron {count} archivo(s)."
    if result == "partial":
        failures = _as_int(files_failed)
        if failures:
            return f"La ejecución terminó con {failures} archivo(s) fallido(s)."
        return "La ejecución terminó con advertencias que requieren revisión."
    if result == "failed":
        label = run_result_label(
            status,
            files_found=files_found,
            files_downloaded=files_downloaded,
            files_failed=files_failed,
            error_type=error_type,
            error_msg=error_msg,
            warnings=warnings,
        )
        return f"La ejecución no pudo completarse: {label.lower()}."
    if result == "running":
        return "La ejecución continúa procesando el origen remoto."
    if result == "cancelled":
        return "La ejecución fue detenida por solicitud del usuario."
    return "No hay información adicional para este estado."


def enrich_run(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add a descriptive result to a run mapping."""
    result = dict(row)
    result["warnings"] = _run_messages(result, "warnings")
    result["notices"] = _run_messages(result, "notices")
    discovery_scope = _run_discovery_scope(result)
    result["discovery_scope"] = discovery_scope
    result_status = run_result_status(
        result.get("status"),
        files_found=result.get("files_found"),
        files_downloaded=result.get("files_downloaded"),
        files_failed=result.get("files_failed"),
        error_type=result.get("error_type"),
        error_msg=result.get("error_msg"),
        warnings=result["warnings"],
    )
    result["result_status"] = result_status
    result["status_label"] = run_result_label(
        result.get("status"),
        files_found=result.get("files_found"),
        files_downloaded=result.get("files_downloaded"),
        files_failed=result.get("files_failed"),
        error_type=result.get("error_type"),
        error_msg=result.get("error_msg"),
        warnings=result["warnings"],
    )
    result["status_detail"] = run_result_detail(
        result.get("status"),
        files_found=result.get("files_found"),
        files_downloaded=result.get("files_downloaded"),
        files_failed=result.get("files_failed"),
        error_type=result.get("error_type"),
        error_msg=result.get("error_msg"),
        warnings=result["warnings"],
        discovery_scope=discovery_scope,
        scan_mode=result.get("scan_mode"),
    )
    return result


def _run_messages(result: dict[str, Any], field: str) -> list[str]:
    """Decode a persisted message list while tolerating legacy rows."""
    existing = result.get(field)
    raw = result.pop(f"{field}_json", None)
    value: object = existing
    if raw is not None:
        try:
            value = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            value = []
    if not isinstance(value, (list, tuple)):
        return []
    messages: list[str] = []
    for item in value:
        message = str(item).strip()
        if message and message not in messages:
            messages.append(message)
    return messages


def _is_legacy_warning_only_partial(
    status: object,
    *,
    files_failed: object,
    error_type: object,
    error_msg: object,
    warnings: object,
) -> bool:
    """Identify historical partials caused only by non-fatal notices."""
    return (
        str(status or "") == "partial"
        and _as_int(files_failed) == 0
        and not str(error_type or "").strip()
        and not str(error_msg or "").strip()
        and not _has_messages(warnings)
    )


def _has_messages(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, (list, tuple)):
        return False
    return any(str(item).strip() for item in value)


def _run_discovery_scope(result: dict[str, Any]) -> dict[str, Any] | None:
    """Decode only the scope persisted with this run, never current config."""
    existing = result.get("discovery_scope")
    raw_paths = result.pop("discovery_paths_json", None)
    raw_recursive = result.pop("discovery_recursive", None)
    raw_max_depth = result.pop("discovery_max_depth", None)
    if existing is not None:
        return _normalize_discovery_scope(existing)
    if (
        raw_paths is None
        or raw_recursive is None
        or raw_max_depth is None
    ):
        return None
    try:
        remote_paths = json.loads(str(raw_paths))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return _normalize_discovery_scope(
        {
            "remote_paths": remote_paths,
            "recursive": raw_recursive,
            "max_depth": raw_max_depth,
        }
    )


def _normalize_discovery_scope(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    remote_paths = value.get("remote_paths")
    recursive = value.get("recursive")
    max_depth = value.get("max_depth")
    if not isinstance(remote_paths, (list, tuple)) or any(
        not isinstance(path, str) for path in remote_paths
    ):
        return None
    if recursive not in (False, True, 0, 1):
        return None
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        return None
    if max_depth < 0:
        return None
    return {
        "remote_paths": list(remote_paths),
        "recursive": bool(recursive),
        "max_depth": max_depth,
    }


def _empty_discovery_detail(
    scope: Mapping[str, Any],
    *,
    scan_mode: object,
) -> str:
    remote_paths = scope.get("remote_paths")
    path_count = len(remote_paths) if isinstance(remote_paths, list) else 0
    if path_count == 1:
        roots = "la ruta configurada"
    elif path_count > 1:
        roots = f"las {path_count} rutas configuradas"
    else:
        roots = "el alcance remoto configurado"

    prefix = (
        "La conexión respondió correctamente, pero no se encontraron "
        "archivos"
    )
    if str(scan_mode or "") == "full_local_reconciliation":
        return f"{prefix} en el árbol remoto de {roots}."
    if not bool(scope.get("recursive")):
        return (
            f"{prefix} directamente en {roots}. Las subcarpetas no se "
            "exploraron en esta corrida."
        )
    max_depth = int(scope.get("max_depth") or 0)
    if max_depth == 0:
        return (
            f"{prefix} directamente en {roots}. La profundidad máxima fue "
            "0, por lo que no se exploraron subcarpetas."
        )
    return (
        f"{prefix} en {roots} ni en las subcarpetas exploradas hasta "
        f"{max_depth} nivel(es) de profundidad."
    )


def file_status_label(
    status: object,
    error_type: object = None,
    plan_status: object = None,
) -> str:
    """Return a descriptive file-transfer label."""
    canonical = str(status or "")
    error_code = str(error_type or "")
    if canonical == "failed" and error_code:
        return RUN_ERROR_LABELS.get(error_code, FILE_STATUS_LABELS["failed"])
    plan_code = str(plan_status or "")
    if canonical in {"skipped", "duplicate"} and plan_code:
        return PLAN_STATUS_LABELS.get(
            plan_code, FILE_STATUS_LABELS.get(canonical, canonical)
        )
    return FILE_STATUS_LABELS.get(canonical, "Estado no identificado")


def enrich_file(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add a descriptive label to a run-file mapping."""
    result = dict(row)
    result["status_label"] = file_status_label(
        result.get("status"),
        result.get("error_type"),
        result.get("plan_status"),
    )
    return result


def alert_status_label(status: object) -> str:
    """Return a delivery-specific alert label."""
    return ALERT_STATUS_LABELS.get(
        str(status or ""), "Estado de envío no identificado"
    )


def enrich_alert(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add a descriptive delivery label to an alert mapping."""
    result = dict(row)
    result["status_label"] = alert_status_label(result.get("status"))
    return result


def plan_status_label(status: object) -> str:
    """Return a descriptive dry-run item label."""
    return PLAN_STATUS_LABELS.get(
        str(status or ""), "Resultado de planificación no identificado"
    )


def plan_result_status(
    *,
    files_found: object,
    files_planned: object,
    is_partial: object = False,
) -> str:
    """Classify a dry-run summary based on listed and eligible files."""
    if bool(is_partial):
        return "partial"
    if _as_int(files_found) == 0:
        return "no_files"
    if _as_int(files_planned) == 0:
        return "no_changes"
    return "files_ready"


def plan_result_label(
    *,
    files_found: object,
    files_planned: object,
    is_partial: object = False,
) -> str:
    """Return the operator-facing dry-run summary label."""
    return PLAN_RESULT_LABELS[
        plan_result_status(
            files_found=files_found,
            files_planned=files_planned,
            is_partial=is_partial,
        )
    ]


def enrich_plan(row: Mapping[str, Any]) -> dict[str, Any]:
    """Add a descriptive result to an already serialized dry-run plan."""
    result = dict(row)
    items = [
        {
            **dict(item),
            "status_label": plan_status_label(item.get("status")),
        }
        for item in result.get("items", [])
    ]
    files_found = result.get("files_found", len(items))
    files_planned = result.get("files_to_download", 0)
    result["items"] = items
    result["result_status"] = plan_result_status(
        files_found=files_found,
        files_planned=files_planned,
        is_partial=result.get("is_partial", False),
    )
    result["result_label"] = plan_result_label(
        files_found=files_found,
        files_planned=files_planned,
        is_partial=result.get("is_partial", False),
    )
    return result


def _as_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
