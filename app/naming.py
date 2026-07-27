"""Safe Windows destination naming and template expansion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from zoneinfo import ZoneInfo

from app.errors import ErrorType, HarvesterError
from app.models import ConflictMode, Connection
from app.transports.base import RemoteFile


_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
MAX_WINDOWS_PATH = 259


@dataclass(frozen=True)
class Destination:
    root: Path
    path: Path
    was_truncated: bool = False


def sanitize_windows_segment(value: str) -> str:
    """Sanitize one path component while preserving useful readability."""
    if value in {".", ".."}:
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "La ruta contiene un segmento de navegación no permitido.",
        )
    sanitized = _INVALID_WINDOWS.sub("_", value).rstrip(" .")
    if not sanitized:
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "Un componente de la ruta queda vacío después del saneamiento.",
        )
    stem = sanitized.split(".", 1)[0].upper()
    if stem in _RESERVED:
        sanitized = "_" + sanitized
    return sanitized


def validate_remote_path(remote_path: str) -> tuple[str, ...]:
    """Reject traversal/absolute Windows paths and return POSIX components."""
    if not remote_path or "\x00" in remote_path:
        raise HarvesterError(ErrorType.PATH_INVALID, "La ruta remota no es válida.")
    if remote_path.startswith("\\\\") or _DRIVE_ABSOLUTE.match(remote_path):
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "El servidor devolvió una ruta absoluta de Windows.",
        )
    normalized = remote_path.replace("\\", "/")
    components = tuple(
        component for component in PurePosixPath(normalized).parts if component != "/"
    )
    if not components or any(component in {".", ".."} for component in components):
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "El servidor devolvió una ruta con navegación '..' o sin nombre.",
        )
    return components


def resolve_destination_root(connection: Connection, portable_root: Path) -> Path:
    configured = Path(connection.dest_root).expanduser()
    root = configured if configured.is_absolute() else portable_root / configured
    return root.resolve(strict=False)


def build_destination(
    connection: Connection,
    remote_file: RemoteFile,
    *,
    portable_root: Path,
    run_id: int,
) -> Destination:
    """Expand a destination template and prove it remains below dest_root."""
    components = validate_remote_path(remote_file.remote_path)
    root = resolve_destination_root(connection, portable_root)
    modified = (remote_file.mtime_utc or datetime.now(timezone.utc)).astimezone(
        ZoneInfo(connection.timezone)
    )
    filename = sanitize_windows_segment(components[-1])
    filename_path = PureWindowsPath(filename)
    remote_dirs = tuple(sanitize_windows_segment(part) for part in components[:-1])
    tokens = {
        "client": sanitize_windows_segment(connection.client or "sin-cliente"),
        "connection": sanitize_windows_segment(connection.name),
        "protocol": connection.protocol.value,
        "yyyy": f"{modified.year:04d}",
        "MM": f"{modified.month:02d}",
        "dd": f"{modified.day:02d}",
        "HH": f"{modified.hour:02d}",
        "remote_dir": "/".join(remote_dirs),
        "filename": filename,
        "basename": sanitize_windows_segment(filename_path.stem),
        "ext": filename_path.suffix,
        "run_id": str(run_id),
    }
    template = connection.dest_template.strip()
    if "{root}" in template:
        if not template.replace("\\", "/").startswith("{root}/"):
            raise HarvesterError(
                ErrorType.PATH_INVALID,
                "El token {root} solo puede aparecer al inicio de la plantilla.",
            )
        template = template.replace("{root}", "", 1).lstrip("/\\")
    try:
        rendered = template.format(root=str(root), **tokens)
    except (KeyError, ValueError) as exc:
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            f"La plantilla de destino contiene un token no válido: {exc}.",
        ) from exc
    if _DRIVE_ABSOLUTE.match(rendered) or rendered.startswith(("/", "\\")):
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "La plantilla produjo una ruta absoluta fuera del destino.",
        )
    rendered_parts = [
        part for part in re.split(r"[/\\]+", rendered) if part
    ]
    if not rendered_parts:
        raise HarvesterError(
            ErrorType.PATH_INVALID, "La plantilla de destino quedó vacía."
        )
    safe_parts = [sanitize_windows_segment(part) for part in rendered_parts]
    candidate = root.joinpath(*safe_parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "La ruta final intenta escapar de la carpeta de destino.",
        )
    fitted, truncated = _fit_windows_path(candidate)
    return Destination(root=root, path=fitted, was_truncated=truncated)


def resolve_conflict(
    path: Path,
    mode: ConflictMode,
    *,
    timestamp: datetime,
) -> Path | None:
    """Return the writable path, or None when the existing file must be skipped."""
    if not path.exists():
        return path
    if mode == ConflictMode.SKIP:
        return None
    if mode == ConflictMode.OVERWRITE:
        return path
    suffix = timestamp.astimezone(timezone.utc).strftime("__%Y%m%d%H%M%S")
    candidate = path.with_name(f"{path.stem}{suffix}{path.suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem}{suffix}_{counter}{path.suffix}"
        )
        counter += 1
    return _fit_windows_path(candidate)[0]


def _fit_windows_path(path: Path) -> tuple[Path, bool]:
    if len(str(path)) <= MAX_WINDOWS_PATH:
        return path, False
    suffix = path.suffix
    allowed_name_length = len(path.name) - (len(str(path)) - MAX_WINDOWS_PATH)
    digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:8]
    marker = f"__{digest}"
    stem_length = allowed_name_length - len(suffix) - len(marker)
    if stem_length < 1:
        raise HarvesterError(
            ErrorType.PATH_INVALID,
            "La ruta supera MAX_PATH y sus carpetas no permiten truncar el nombre.",
        )
    fitted = path.with_name(path.stem[:stem_length] + marker + suffix)
    return fitted, True
