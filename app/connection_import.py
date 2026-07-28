"""Safe connection import for StabilityMonitor and Recolecta backups."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.db import ConnectionRepository
from app.models import AuthType, Connection, Protocol


SUPPORTED_PROTOCOLS = frozenset(protocol.value for protocol in Protocol)
TEXT_FIELDS = frozenset(
    {
        "name",
        "client",
        "protocol",
        "host",
        "username",
        "auth_type",
        "key_path",
        "ssl_mode",
        "window_mode",
        "timezone",
        "schedule_time",
        "dest_root",
        "dest_template",
        "on_conflict",
        "verify_mode",
        "post_action",
        "post_action_path",
        "notes",
    }
)
OPTIONAL_TEXT_FIELDS = frozenset(
    {"key_path", "schedule_time", "post_action_path"}
)
INTEGER_FIELDS = frozenset(
    {
        "port",
        "max_depth",
        "min_size_bytes",
        "max_size_bytes",
        "window_hours",
        "window_overlap_min",
        "quiet_period_s",
        "max_parallel_files",
        "bandwidth_limit_kbps",
        "retries",
    }
)
OPTIONAL_INTEGER_FIELDS = frozenset(
    {"port", "min_size_bytes", "max_size_bytes", "bandwidth_limit_kbps"}
)
BOOLEAN_FIELDS = frozenset({"recursive", "enabled"})
SQLITE_INTEGER_MIN = -(2**63)
SQLITE_INTEGER_MAX = 2**63 - 1


@dataclass(frozen=True)
class ImportNotice:
    index: int
    name: str
    protocol: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "name": self.name,
            "protocol": self.protocol,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ConnectionImportResult:
    source_app: str
    total: int
    created: tuple[Connection, ...]
    skipped: tuple[ImportNotice, ...]
    errors: tuple[ImportNotice, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_app": self.source_app,
            "total": self.total,
            "created_count": len(self.created),
            "skipped_count": len(self.skipped),
            "error_count": len(self.errors),
            "created": [item.to_public_dict() for item in self.created],
            "skipped": [item.to_dict() for item in self.skipped],
            "errors": [item.to_dict() for item in self.errors],
        }


def import_connections(
    backup: Mapping[str, Any],
    repository: ConnectionRepository,
) -> ConnectionImportResult:
    """Import supported file connections and report every omitted entry."""
    if not isinstance(backup, Mapping):
        raise ValueError("El backup debe ser un objeto JSON.")
    source_app = str(backup.get("app", "")).strip()
    if source_app not in {"StabilityMonitor", "Recolecta"}:
        raise ValueError(
            "El archivo debe ser un backup de StabilityMonitor o Recolecta."
        )
    raw_connections = backup.get("connections")
    if not isinstance(raw_connections, list):
        raise ValueError("El backup debe incluir una lista connections.")

    fingerprints = {
        _fingerprint(connection) for connection in repository.list()
    }
    created: list[Connection] = []
    skipped: list[ImportNotice] = []
    errors: list[ImportNotice] = []

    for index, raw in enumerate(raw_connections):
        if not isinstance(raw, Mapping):
            errors.append(
                ImportNotice(index, f"Entrada {index + 1}", "", "No es un objeto.")
            )
            continue
        name = str(raw.get("name", "")).strip() or f"Entrada {index + 1}"
        protocol = str(raw.get("protocol", "")).strip().upper()
        if protocol not in SUPPORTED_PROTOCOLS:
            skipped.append(
                ImportNotice(
                    index,
                    name,
                    protocol or "SIN PROTOCOLO",
                    f"Protocolo no compatible: {protocol or 'sin valor'}.",
                )
            )
            continue
        try:
            candidate, secret = _connection_from_backup(
                raw,
                source_app=source_app,
            )
            normalized = candidate.normalized()
            fingerprint = _fingerprint(normalized)
            if fingerprint in fingerprints:
                skipped.append(
                    ImportNotice(
                        index,
                        normalized.name,
                        normalized.protocol.value,
                        "La misma conexión ya existe.",
                    )
                )
                continue
            saved = repository.create(normalized, secret=secret)
        except (TypeError, ValueError, OverflowError) as exc:
            errors.append(ImportNotice(index, name, protocol, str(exc)))
            continue
        created.append(saved)
        fingerprints.add(fingerprint)

    return ConnectionImportResult(
        source_app=source_app,
        total=len(raw_connections),
        created=tuple(created),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def _connection_from_backup(
    raw: Mapping[str, Any],
    *,
    source_app: str,
) -> tuple[Connection, str | None]:
    secret_value = raw.get("secret")
    if secret_value is not None and not isinstance(secret_value, str):
        raise ValueError("El secreto debe ser texto si está presente.")
    secret = secret_value if secret_value else None

    if source_app == "Recolecta":
        values = {
            key: raw[key]
            for key in Connection.MUTABLE_FIELDS
            if key in raw
        }
        values["remote_paths"] = _json_string_list(
            raw.get("remote_paths", ()),
            field_name="remote_paths",
        )
        for key in ("include_globs", "exclude_globs"):
            if key in values:
                values[key] = _json_string_list(
                    values[key],
                    field_name=key,
                )
    else:
        values = {
            "name": raw.get("name", ""),
            "client": raw.get("client", ""),
            "protocol": raw.get("protocol", ""),
            "host": raw.get("host", ""),
            "port": _optional_port(raw.get("port")),
            "username": raw.get("username", ""),
            "auth_type": raw.get("auth_type", AuthType.PASSWORD.value),
            "key_path": raw.get("key_path"),
            "ssl_mode": raw.get("ssl_mode", "preferred"),
            "remote_paths": _json_string_list(
                raw.get("targets_json", ()),
                field_name="targets_json",
            ),
            "timeout_s": raw.get("timeout_s", 30),
            "retries": raw.get("retries", 3),
            "enabled": _as_bool(raw.get("enabled", True)),
            "notes": raw.get("notes", ""),
        }

    _validate_text_fields(values)
    _normalize_scalar_fields(values)
    if secret is None:
        values["enabled"] = False
    return Connection(**values), secret


def _validate_text_fields(values: Mapping[str, Any]) -> None:
    for field_name in TEXT_FIELDS.intersection(values):
        value = values[field_name]
        if value is None and field_name in OPTIONAL_TEXT_FIELDS:
            continue
        if not isinstance(value, str):
            raise ValueError(f"El campo {field_name} debe ser texto.")


def _normalize_scalar_fields(values: dict[str, Any]) -> None:
    for field_name in INTEGER_FIELDS.intersection(values):
        values[field_name] = _integer_value(
            values[field_name],
            field_name=field_name,
            optional=field_name in OPTIONAL_INTEGER_FIELDS,
        )
    if "timeout_s" in values:
        values["timeout_s"] = _finite_float(
            values["timeout_s"],
            field_name="timeout_s",
        )
    for field_name in BOOLEAN_FIELDS.intersection(values):
        values[field_name] = _as_bool(values[field_name])


def _json_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} no contiene JSON válido.") from exc
    if parsed is None:
        return ()
    if not isinstance(parsed, (list, tuple)):
        raise ValueError(f"{field_name} debe contener una lista.")
    if any(not isinstance(item, str) for item in parsed):
        raise ValueError(f"{field_name} debe contener únicamente textos.")
    return tuple(item for item in parsed if item.strip())


def _optional_port(value: Any) -> int | None:
    return _integer_value(value, field_name="port", optional=True)


def _integer_value(
    value: Any,
    *,
    field_name: str,
    optional: bool,
) -> int | None:
    if value is None or (optional and value == ""):
        if optional:
            return None
        raise ValueError(f"El campo {field_name} debe ser un número entero.")
    if isinstance(value, bool):
        raise ValueError(f"El campo {field_name} debe ser un número entero.")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(
                f"El campo {field_name} debe ser un número entero finito."
            )
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.lstrip("+-").isdigit():
            raise ValueError(
                f"El campo {field_name} debe ser un número entero."
            )
        value = text
    elif not isinstance(value, int):
        raise ValueError(f"El campo {field_name} debe ser un número entero.")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"El campo {field_name} debe ser un número entero."
        ) from exc
    if not SQLITE_INTEGER_MIN <= result <= SQLITE_INTEGER_MAX:
        raise ValueError(
            f"El campo {field_name} excede el rango entero permitido."
        )
    return result


def _finite_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"El campo {field_name} debe ser un número finito.")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"El campo {field_name} debe ser un número finito."
        ) from exc
    if not math.isfinite(result):
        raise ValueError(f"El campo {field_name} debe ser un número finito.")
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
        raise ValueError(f"Valor booleano no válido: {value}.")
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    raise ValueError(f"Valor booleano no válido: {value}.")


def _fingerprint(connection: Connection) -> tuple[str, str, str, int | None]:
    return (
        connection.name.casefold(),
        connection.protocol.value,
        connection.host.casefold(),
        connection.port,
    )
