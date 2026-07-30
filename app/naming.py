"""Safe Windows destination naming and template expansion."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from zoneinfo import ZoneInfo

from app.errors import ErrorType, RecolectaError
from app.models import ConflictMode, Connection, Protocol
from app.transports.base import RemoteFile


_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
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


@dataclass(frozen=True)
class _RemotePath:
    kind: str
    anchor: str
    components: tuple[str, ...]

    @property
    def case_insensitive(self) -> bool:
        return self.kind in {"smb", "local_windows"}


def sanitize_windows_segment(value: str) -> str:
    """Sanitize one path component while preserving useful readability."""
    if value in {".", ".."}:
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "La ruta contiene un segmento de navegación no permitido.",
        )
    sanitized = _INVALID_WINDOWS.sub("_", value).rstrip(" .")
    if not sanitized:
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "Un componente de la ruta queda vacío después del saneamiento.",
        )
    stem = sanitized.split(".", 1)[0].upper()
    if stem in _RESERVED:
        sanitized = "_" + sanitized
    return sanitized


def validate_remote_path(remote_path: str) -> tuple[str, ...]:
    """Reject traversal/absolute Windows paths and return POSIX components."""
    value = _validate_path_text(remote_path)
    if value.startswith(("\\\\", "//")) or _WINDOWS_DRIVE.match(value):
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "El servidor devolvió una ruta absoluta de Windows.",
        )
    return _split_components(value)


def _remote_tree_components(
    connection: Connection, remote_path: str
) -> tuple[str, ...]:
    """Return a safe remote hierarchy rooted below the local destination."""
    parsed = _parse_remote_path(connection, remote_path)
    matched_root = _longest_matching_root(connection, parsed)
    if parsed.kind in {"local_windows", "local_posix"}:
        if matched_root is None:
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "Una ruta local absoluta requiere una raíz SMB configurada.",
            )
        relative = parsed.components[len(matched_root.components) :]
        raw_tree = (matched_root.components[-1], *relative)
    else:
        raw_tree = parsed.components
    return tuple(
        _sanitize_remote_tree_segment(
            part,
            is_filename=index == len(raw_tree) - 1,
        )
        for index, part in enumerate(raw_tree)
    )


def _parse_remote_path(connection: Connection, value: str) -> _RemotePath:
    text = _validate_path_text(value)
    is_smb = connection.protocol == Protocol.SMB
    normalized = text.replace("\\", "/")
    if normalized.startswith("//"):
        if not is_smb:
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "El servidor devolvió una ruta UNC para un protocolo no SMB.",
            )
        parts = _split_components(normalized[2:])
        if len(parts) < 2:
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "La ruta UNC debe incluir servidor y recurso compartido.",
            )
        host, components = parts[0], parts[1:]
        if host.casefold() != connection.host.casefold():
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "La ruta UNC pertenece a un servidor distinto al configurado.",
            )
        return _RemotePath("smb", connection.host.casefold(), components)
    if _WINDOWS_DRIVE.match(text):
        if not is_smb or not _DRIVE_ABSOLUTE.match(text):
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "El servidor devolvió una ruta absoluta de Windows.",
            )
        windows = PureWindowsPath(text)
        return _RemotePath(
            "local_windows",
            windows.drive.casefold(),
            _checked_components(windows.parts[1:]),
        )
    if (
        is_smb
        and normalized.startswith("/")
        and _matches_configured_posix_fixture(connection, normalized)
    ):
        return _RemotePath(
            "local_posix",
            "/",
            _split_components(normalized),
        )
    kind = "smb" if is_smb else "posix"
    anchor = connection.host.casefold() if is_smb else "/"
    return _RemotePath(kind, anchor, _split_components(normalized))


def _matches_configured_posix_fixture(
    connection: Connection, remote_path: str
) -> bool:
    remote_parts = _split_components(remote_path)
    for configured in connection.remote_paths:
        normalized = configured.replace("\\", "/")
        if not normalized.startswith("/") or normalized.startswith("//"):
            continue
        configured_parts = _split_components(normalized)
        if remote_parts[: len(configured_parts)] == configured_parts:
            return True
    return False


def _longest_matching_root(
    connection: Connection, remote: _RemotePath
) -> _RemotePath | None:
    matches: list[_RemotePath] = []
    for configured in connection.remote_paths:
        root = _parse_remote_path(connection, configured)
        if root.kind != remote.kind or root.anchor != remote.anchor:
            continue
        if len(root.components) > len(remote.components):
            continue
        left = remote.components[: len(root.components)]
        if remote.case_insensitive:
            equal = tuple(part.casefold() for part in left) == tuple(
                part.casefold() for part in root.components
            )
        else:
            equal = left == root.components
        if equal:
            matches.append(root)
    return max(matches, key=lambda item: len(item.components), default=None)


def _validate_path_text(value: str) -> str:
    if not value or "\x00" in value:
        raise RecolectaError(ErrorType.PATH_INVALID, "La ruta remota no es válida.")
    return value


def _split_components(value: str) -> tuple[str, ...]:
    raw = tuple(part for part in value.replace("\\", "/").split("/") if part)
    return _checked_components(raw)


def _checked_components(parts: tuple[str, ...]) -> tuple[str, ...]:
    if not parts or any(part in {".", ".."} for part in parts):
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "El servidor devolvió una ruta con navegación '..' o sin nombre.",
        )
    return parts


def _sanitize_remote_tree_segment(value: str, *, is_filename: bool) -> str:
    sanitized = sanitize_windows_segment(value)
    if sanitized == value:
        return sanitized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    if not is_filename:
        return f"{sanitized}__{digest}"
    path = PureWindowsPath(sanitized)
    return f"{path.stem}__{digest}{path.suffix}"


def resolve_destination_root(connection: Connection, portable_root: Path) -> Path:
    configured = Path(connection.dest_root).expanduser()
    root = configured if configured.is_absolute() else portable_root / configured
    return root.resolve(strict=False)


def local_path_key(path: Path) -> bytes:
    """Return a Windows-equivalent, fixed-size key for a local path."""
    normalized = unicodedata.normalize(
        "NFC", str(path.resolve(strict=False)).replace("/", "\\")
    )
    return hashlib.sha256(
        normalized.casefold().encode("utf-8", errors="surrogatepass")
    ).digest()


def collision_path(candidate: Path, marker: str) -> Path:
    """Append a stable marker while keeping the path within MAX_PATH."""
    suffix = candidate.suffix
    parent_length = len(str(candidate.parent)) + 1
    available_stem = MAX_WINDOWS_PATH - parent_length - len(marker) - len(suffix)
    if available_stem < 1:
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "La ruta de destino no permite desambiguar una colisión.",
        )
    return candidate.with_name(
        f"{candidate.stem[:available_stem]}{marker}{suffix}"
    )


def build_destination(
    connection: Connection,
    remote_file: RemoteFile,
    *,
    portable_root: Path,
    run_id: int,
    fallback_time: datetime | None = None,
) -> Destination:
    """Expand a destination template and prove it remains below dest_root."""
    parsed_remote = _parse_remote_path(connection, remote_file.remote_path)
    components = parsed_remote.components
    template = connection.dest_template.strip()
    remote_tree = (
        _remote_tree_components(connection, remote_file.remote_path)
        if "{remote_tree}" in template
        else ()
    )
    root = resolve_destination_root(connection, portable_root)
    fallback = fallback_time or datetime.now(timezone.utc)
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=timezone.utc)
    modified = (remote_file.mtime_utc or fallback).astimezone(
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
        "remote_tree": "/".join(remote_tree),
        "filename": filename,
        "basename": sanitize_windows_segment(filename_path.stem),
        "ext": filename_path.suffix,
        "run_id": str(run_id),
    }
    if "{root}" in template:
        if not template.replace("\\", "/").startswith("{root}/"):
            raise RecolectaError(
                ErrorType.PATH_INVALID,
                "El token {root} solo puede aparecer al inicio de la plantilla.",
            )
        template = template.replace("{root}", "", 1).lstrip("/\\")
    try:
        rendered = template.format(root=str(root), **tokens)
    except (KeyError, ValueError) as exc:
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            f"La plantilla de destino contiene un token no válido: {exc}.",
        ) from exc
    if _DRIVE_ABSOLUTE.match(rendered) or rendered.startswith(("/", "\\")):
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "La plantilla produjo una ruta absoluta fuera del destino.",
        )
    rendered_parts = [
        part for part in re.split(r"[/\\]+", rendered) if part
    ]
    if not rendered_parts:
        raise RecolectaError(
            ErrorType.PATH_INVALID, "La plantilla de destino quedó vacía."
        )
    safe_parts = [sanitize_windows_segment(part) for part in rendered_parts]
    candidate = root.joinpath(*safe_parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise RecolectaError(
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
        raise RecolectaError(
            ErrorType.PATH_INVALID,
            "La ruta supera MAX_PATH y sus carpetas no permiten truncar el nombre.",
        )
    fitted = path.with_name(path.stem[:stem_length] + marker + suffix)
    return fitted, True
