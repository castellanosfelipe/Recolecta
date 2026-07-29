"""Pre-save validation for remote connections and local destinations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.errors import (
    ErrorType,
    RecolectaError,
    classify_exception,
    is_retryable,
)
from app.models import Connection, PostAction
from app.naming import build_destination, resolve_destination_root
from app.transports import create_transport
from app.transports.base import RemoteFile, Transport


TransportFactory = Callable[..., Transport]


@dataclass(frozen=True)
class ConnectionValidationResult:
    """Evidence that both sides of a connection draft are usable."""

    local_path: str
    remote_paths: tuple[str, ...]
    remote_files_found: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": True,
            "local_path": self.local_path,
            "remote_paths": list(self.remote_paths),
            "remote_files_found": self.remote_files_found,
            "warnings": list(self.warnings),
        }


def validate_connection_paths(
    connection: Connection,
    *,
    secret: str | None,
    portable_root: Path,
    known_hosts: Path,
    transport_factory: TransportFactory = create_transport,
) -> ConnectionValidationResult:
    """Authenticate, list every remote root, and prove local write access."""
    normalized = connection.normalized()
    if not normalized.remote_paths:
        raise ValueError("Ingrese al menos una ruta remota para validarla.")

    local_path = _validate_local_destination(
        normalized,
        portable_root=portable_root,
    )
    try:
        transport = transport_factory(
            normalized,
            secret=secret,
            known_hosts=known_hosts,
        )
        try:
            transport.connect()
            listing = transport.list_files(
                normalized.remote_paths,
                recursive=False,
                max_depth=0,
            )
            warnings = list(listing.warnings)
            if (
                normalized.post_action == PostAction.MOVE_REMOTE
                and normalized.post_action_path
                and normalized.post_action_path
                not in normalized.remote_paths
            ):
                move_listing = transport.list_files(
                    (normalized.post_action_path,),
                    recursive=False,
                    max_depth=0,
                )
                warnings.extend(
                    warning
                    for warning in move_listing.warnings
                    if warning not in warnings
                )
        finally:
            transport.close()
    except Exception as exc:
        raise connection_validation_error(exc) from exc

    return ConnectionValidationResult(
        local_path=str(local_path),
        remote_paths=normalized.remote_paths,
        remote_files_found=len(listing.files),
        warnings=tuple(warnings),
    )


def _validate_local_destination(
    connection: Connection,
    *,
    portable_root: Path,
) -> Path:
    try:
        root = resolve_destination_root(connection, portable_root)
        if root.exists() and not root.is_dir():
            raise RecolectaError(
                ErrorType.DISK_WRITE,
                f"El destino local no es una carpeta: {root}",
            )
        sample_file = RemoteFile(
            "/recolecta-validacion.bin",
            0,
            datetime.now(timezone.utc),
        )
        destination = build_destination(
            connection,
            sample_file,
            portable_root=portable_root,
            run_id=0,
        )
        probe_directory = destination.path.parent
        if probe_directory.exists() and not probe_directory.is_dir():
            raise RecolectaError(
                ErrorType.DISK_WRITE,
                (
                    "Una carpeta de la ruta local es un archivo: "
                    f"{probe_directory}"
                ),
            )
    except RecolectaError:
        raise
    except OSError as exc:
        raise RecolectaError(
            ErrorType.DISK_WRITE,
            "No se pudo resolver o revisar el destino local configurado.",
        ) from exc
    missing_directories: list[Path] = []
    try:
        cursor = probe_directory
        while not cursor.exists():
            missing_directories.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
    except OSError as exc:
        raise RecolectaError(
            ErrorType.DISK_WRITE,
            "No se pudo revisar la estructura del destino local.",
        ) from exc

    probe_id = uuid4().hex
    probe = probe_directory / f".recolecta-validacion-{probe_id}.tmp"
    renamed_probe = probe_directory / f".recolecta-validacion-{probe_id}.ok"
    cleanup_error: OSError | None = None
    try:
        probe_directory.mkdir(parents=True, exist_ok=True)
        if not probe_directory.is_dir():
            raise RecolectaError(
                ErrorType.DISK_WRITE,
                f"El destino local no es una carpeta: {probe_directory}",
            )
        with probe.open("xb") as stream:
            stream.write(b"recolecta-path-validation")
            stream.flush()
        probe.replace(renamed_probe)
    except RecolectaError:
        raise
    except OSError as exc:
        raise RecolectaError(
            ErrorType.DISK_WRITE,
            f"No se puede escribir en el destino local: {probe_directory}",
        ) from exc
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_error = exc
        try:
            renamed_probe.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        for directory in missing_directories:
            try:
                directory.rmdir()
            except OSError:
                break
        if cleanup_error is not None:
            raise RecolectaError(
                ErrorType.DISK_WRITE,
                (
                    "Se pudo escribir en el destino local, pero no fue posible "
                    "eliminar el archivo temporal de validación."
                ),
            ) from cleanup_error
    return root


def connection_validation_error(exc: Exception) -> RecolectaError:
    """Convert a transport failure into a secret-free actionable error."""
    if isinstance(exc, RecolectaError):
        return exc
    error_type = classify_exception(exc)
    messages = {
        ErrorType.DNS: "No se pudo resolver el servidor remoto.",
        ErrorType.TCP_CONNECT: "No se pudo conectar con el servidor remoto.",
        ErrorType.TCP_TIMEOUT: "El servidor remoto agotó el tiempo de espera.",
        ErrorType.AUTH: "La credencial fue rechazada por el servidor remoto.",
        ErrorType.TLS: "No se pudo validar la seguridad TLS/SSH del servidor.",
        ErrorType.PERMISSION: (
            "La credencial no tiene permiso para acceder a las rutas remotas."
        ),
        ErrorType.TARGET_MISSING: (
            "Una de las rutas remotas configuradas no existe."
        ),
        ErrorType.PROTOCOL: (
            "El servidor rechazó la operación de validación del protocolo."
        ),
    }
    message = messages.get(
        error_type,
        (
            "No fue posible validar las rutas remotas. "
            "Revise servidor, puerto, credencial y rutas."
        ),
    )
    return RecolectaError(
        error_type,
        message,
        retryable=is_retryable(error_type),
    )
