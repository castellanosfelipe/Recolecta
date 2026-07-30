"""Domain models for connections and persisted run state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Any, ClassVar


def utc_now_iso() -> str:
    """Return a stable UTC timestamp suitable for SQLite text columns."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Protocol(StrEnum):
    FTP = "FTP"
    FTPS = "FTPS"
    SFTP = "SFTP"
    WEBDAV = "WEBDAV"
    WEBDAVS = "WEBDAVS"
    SMB = "SMB"


DEFAULT_PORTS: dict[Protocol, int] = {
    Protocol.FTP: 21,
    Protocol.FTPS: 21,
    Protocol.SFTP: 22,
    Protocol.WEBDAV: 80,
    Protocol.WEBDAVS: 443,
    Protocol.SMB: 445,
}


class AuthType(StrEnum):
    PASSWORD = "password"
    KEY = "key"


class WindowMode(StrEnum):
    CALENDAR_DAY = "calendar_day"
    ROLLING_HOURS = "rolling_hours"
    SINCE_LAST_RUN = "since_last_run"


class ConflictMode(StrEnum):
    SKIP = "skip"
    OVERWRITE = "overwrite"
    KEEP_BOTH = "keep_both"


class VerifyMode(StrEnum):
    SIZE = "size"
    SHA256 = "sha256"


class PostAction(StrEnum):
    NONE = "none"
    MOVE_REMOTE = "move_remote"
    DELETE_REMOTE = "delete_remote"


@dataclass(frozen=True)
class Connection:
    """Public connection model; encrypted secret material is never exposed."""

    MUTABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "client",
            "protocol",
            "host",
            "port",
            "username",
            "auth_type",
            "key_path",
            "ssl_mode",
            "remote_paths",
            "recursive",
            "max_depth",
            "include_globs",
            "exclude_globs",
            "min_size_bytes",
            "max_size_bytes",
            "window_mode",
            "window_hours",
            "window_overlap_min",
            "quiet_period_s",
            "timezone",
            "schedule_time",
            "dest_root",
            "dest_template",
            "full_local_reconciliation",
            "on_conflict",
            "verify_mode",
            "max_parallel_files",
            "bandwidth_limit_kbps",
            "timeout_s",
            "retries",
            "post_action",
            "post_action_path",
            "enabled",
            "notes",
        }
    )

    id: int | None = None
    name: str = ""
    client: str = ""
    protocol: Protocol = Protocol.SFTP
    host: str = ""
    port: int | None = None
    username: str = ""
    auth_type: AuthType = AuthType.PASSWORD
    key_path: str | None = None
    ssl_mode: str = "preferred"
    remote_paths: tuple[str, ...] = field(default_factory=tuple)
    recursive: bool = False
    max_depth: int = 3
    include_globs: tuple[str, ...] = field(default_factory=tuple)
    exclude_globs: tuple[str, ...] = field(default_factory=tuple)
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    window_mode: WindowMode = WindowMode.CALENDAR_DAY
    window_hours: int = 24
    window_overlap_min: int = 15
    quiet_period_s: int = 120
    timezone: str = "America/Bogota"
    schedule_time: str | None = None
    dest_root: str = "downloads"
    dest_template: str = r"{remote_tree}"
    full_local_reconciliation: bool = False
    on_conflict: ConflictMode = ConflictMode.SKIP
    verify_mode: VerifyMode = VerifyMode.SIZE
    max_parallel_files: int = 2
    bandwidth_limit_kbps: int | None = None
    timeout_s: float = 30.0
    retries: int = 3
    post_action: PostAction = PostAction.NONE
    post_action_path: str | None = None
    enabled: bool = True
    notes: str = ""
    has_secret: bool = False
    created_at: str | None = None
    updated_at: str | None = None

    def normalized(self) -> "Connection":
        """Return a normalized, validated copy ready for persistence."""
        try:
            protocol = Protocol(str(self.protocol).upper())
            auth_type = AuthType(self.auth_type)
            window_mode = WindowMode(self.window_mode)
            on_conflict = ConflictMode(self.on_conflict)
            verify_mode = VerifyMode(self.verify_mode)
            post_action = PostAction(self.post_action)
        except ValueError as exc:
            raise ValueError(f"Valor de conexión no válido: {exc}") from exc

        port = self.port if self.port is not None else DEFAULT_PORTS[protocol]
        normalized = replace(
            self,
            name=self.name.strip(),
            client=self.client.strip(),
            protocol=protocol,
            host=self.host.strip(),
            port=port,
            username=self.username.strip(),
            auth_type=auth_type,
            key_path=self.key_path.strip() if self.key_path else None,
            ssl_mode=self.ssl_mode.strip().lower(),
            remote_paths=tuple(str(path).strip() for path in self.remote_paths if str(path).strip()),
            include_globs=tuple(str(value).strip() for value in self.include_globs if str(value).strip()),
            exclude_globs=tuple(str(value).strip() for value in self.exclude_globs if str(value).strip()),
            window_mode=window_mode,
            timezone=self.timezone.strip(),
            schedule_time=_normalize_schedule_time(self.schedule_time),
            dest_root=self.dest_root.strip(),
            dest_template=self.dest_template.strip(),
            on_conflict=on_conflict,
            verify_mode=verify_mode,
            post_action=post_action,
            post_action_path=self.post_action_path.strip() if self.post_action_path else None,
            notes=self.notes.strip(),
        )
        normalized.validate()
        return normalized

    def validate(self) -> None:
        """Raise an actionable Spanish error when the model is invalid."""
        if not self.name:
            raise ValueError("El nombre de la conexión es obligatorio.")
        if not self.host:
            raise ValueError("El host de la conexión es obligatorio.")
        if self.port is None or not 1 <= self.port <= 65535:
            raise ValueError("El puerto debe estar entre 1 y 65535.")
        if not self.dest_root:
            raise ValueError("La carpeta de destino es obligatoria.")
        if not self.dest_template:
            raise ValueError("La plantilla de destino es obligatoria.")
        if (
            self.full_local_reconciliation
            and _template_references_field(self.dest_template, "run_id")
        ):
            raise ValueError(
                "La comparación completa no admite {run_id} en la "
                "plantilla de destino porque impediría comparar una ruta "
                "local estable."
            )
        if self.max_depth < 0:
            raise ValueError("La profundidad máxima no puede ser negativa.")
        if self.window_hours <= 0:
            raise ValueError("La ventana en horas debe ser mayor que cero.")
        if self.window_overlap_min < 0 or self.quiet_period_s < 0:
            raise ValueError("El solape y el periodo de calma no pueden ser negativos.")
        if self.max_parallel_files < 1:
            raise ValueError("Debe existir al menos un trabajador por conexión.")
        if self.bandwidth_limit_kbps is not None and self.bandwidth_limit_kbps <= 0:
            raise ValueError("El límite de ancho de banda debe ser mayor que cero.")
        if self.timeout_s <= 0 or self.retries < 0:
            raise ValueError("El timeout debe ser positivo y los reintentos no negativos.")
        if self.min_size_bytes is not None and self.min_size_bytes < 0:
            raise ValueError("El tamaño mínimo no puede ser negativo.")
        if self.max_size_bytes is not None and self.max_size_bytes < 0:
            raise ValueError("El tamaño máximo no puede ser negativo.")
        if (
            self.min_size_bytes is not None
            and self.max_size_bytes is not None
            and self.min_size_bytes > self.max_size_bytes
        ):
            raise ValueError("El tamaño mínimo no puede superar el máximo.")
        if self.auth_type == AuthType.KEY and not self.key_path:
            raise ValueError("La autenticación por llave requiere una ruta de llave.")
        if self.post_action == PostAction.MOVE_REMOTE and not self.post_action_path:
            raise ValueError("Mover en el servidor requiere una ruta de destino remota.")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError(f"Zona horaria IANA no válida: {self.timezone}") from exc

    def with_changes(self, changes: dict[str, Any]) -> "Connection":
        """Return a validated copy with an explicit mutable-field allowlist."""
        forbidden = set(changes) - self.MUTABLE_FIELDS
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"Campos de conexión no editables: {names}.")
        return replace(self, **changes).normalized()

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without any encrypted or plaintext credential."""
        value = asdict(self)
        value["protocol"] = self.protocol.value
        value["auth_type"] = self.auth_type.value
        value["window_mode"] = self.window_mode.value
        value["on_conflict"] = self.on_conflict.value
        value["verify_mode"] = self.verify_mode.value
        value["post_action"] = self.post_action.value
        value["remote_paths"] = list(self.remote_paths)
        value["include_globs"] = list(self.include_globs)
        value["exclude_globs"] = list(self.exclude_globs)
        return value

    @property
    def destination_path(self) -> Path:
        return Path(self.dest_root)


def _template_references_field(template: str, expected: str) -> bool:
    """Detect a format field, including conversions and nested format specs."""
    for _, field_name, format_spec, _ in Formatter().parse(template):
        if field_name is not None:
            base_name = field_name.split(".", 1)[0].split("[", 1)[0]
            if base_name == expected:
                return True
        if format_spec and _template_references_field(format_spec, expected):
            return True
    return False


def _normalize_schedule_time(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("La hora de la conexión debe usar el formato HH:MM.")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(
            "La hora de la conexión debe estar entre 00:00 y 23:59."
        )
    return f"{hour:02d}:{minute:02d}"
