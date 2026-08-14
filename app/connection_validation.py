"""Pre-save validation for remote connections and local destinations."""

from __future__ import annotations

import ftplib
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
from app.logging_setup import redact_secrets
from app.models import Connection, PostAction
from app.naming import build_destination, resolve_destination_root
from app.transports import create_transport
from app.transports.base import RemoteFile, Transport


TransportFactory = Callable[..., Transport]
REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT = 100


@dataclass(frozen=True)
class ConnectionValidationResult:
    """Evidence that both sides of a connection draft are usable."""

    local_path: str
    remote_paths: tuple[str, ...]
    # Number of file metadata records sampled, not an unbounded inventory.
    remote_files_found: int
    warnings: tuple[str, ...]
    remote_files_found_is_exact: bool = True
    remote_files_sample_limit_per_root: int = (
        REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "valid": True,
            "local_path": self.local_path,
            "remote_paths": list(self.remote_paths),
            "remote_files_found": self.remote_files_found,
            "remote_files_found_is_exact": self.remote_files_found_is_exact,
            "remote_files_sample_limit_per_root": (
                self.remote_files_sample_limit_per_root
            ),
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
    """Authenticate, sample every remote root, and prove local write access.

    Remote validation consumes at most
    ``REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT + 1`` metadata records per root.
    The extra record only detects truncation; ``remote_files_found`` reports
    the retained sample and is exact only when
    ``remote_files_found_is_exact`` is true.
    """
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
            warnings: list[str] = []
            remote_files_found = 0
            remote_files_found_is_exact = True
            for remote_path in normalized.remote_paths:
                try:
                    sampled, truncated = _sample_remote_root(
                        transport,
                        remote_path,
                    )
                except Exception as exc:
                    raise _remote_path_validation_error(
                        exc,
                        remote_path=remote_path,
                    ) from exc
                remote_files_found += sampled
                remote_files_found_is_exact = (
                    remote_files_found_is_exact and not truncated
                )
                _extend_unique(
                    warnings,
                    transport.last_listing_warnings,
                )
                if truncated:
                    _extend_unique(
                        warnings,
                        (_truncated_sample_warning(remote_path),),
                    )
            if (
                normalized.post_action == PostAction.MOVE_REMOTE
                and normalized.post_action_path
                and normalized.post_action_path
                not in normalized.remote_paths
            ):
                _, move_truncated = _sample_remote_root(
                    transport,
                    normalized.post_action_path,
                )
                _extend_unique(
                    warnings,
                    transport.last_listing_warnings,
                )
                if move_truncated:
                    _extend_unique(
                        warnings,
                        (
                            _truncated_sample_warning(
                                normalized.post_action_path,
                                counted=False,
                            ),
                        ),
                    )
        finally:
            transport.close()
    except Exception as exc:
        raise connection_validation_error(exc) from exc

    return ConnectionValidationResult(
        local_path=str(local_path),
        remote_paths=normalized.remote_paths,
        remote_files_found=remote_files_found,
        warnings=tuple(warnings),
        remote_files_found_is_exact=remote_files_found_is_exact,
    )


def _sample_remote_root(
    transport: Transport,
    remote_path: str,
) -> tuple[int, bool]:
    """Validate one root with a bounded metadata-only sample.

    Iterators are explicitly closed even when listing raises, so transports
    can release protocol data streams and disk-backed traversal state before
    their session is closed.
    """
    discovered = transport.iter_files(
        (remote_path,),
        recursive=False,
        max_depth=0,
    )
    sampled = 0
    truncated = False
    try:
        while sampled < REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT:
            try:
                next(discovered)
            except StopIteration:
                return sampled, False
            sampled += 1
        try:
            next(discovered)
        except StopIteration:
            return sampled, False
        truncated = True
    finally:
        close_iterator = getattr(discovered, "close", None)
        if callable(close_iterator):
            close_iterator()
    return sampled, truncated


def _extend_unique(target: list[str], values: tuple[str, ...]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _truncated_sample_warning(
    remote_path: str,
    *,
    counted: bool = True,
) -> str:
    limit = REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT
    count_explanation = (
        "remote_files_found no representa el total exacto."
        if counted
        else (
            "Esta carpeta de movimiento no forma parte de "
            "remote_files_found."
        )
    )
    return (
        f"La ruta remota {remote_path!r} contiene más de {limit} archivos "
        f"en su nivel inicial; se validó una muestra de {limit}. "
        f"{count_explanation}"
    )


def _remote_path_validation_error(
    exc: Exception,
    *,
    remote_path: str,
) -> RecolectaError:
    """Attach a safe root reference without exposing the server response."""
    validation_error = connection_validation_error(exc)
    path_label = _safe_remote_path_label(remote_path)
    safe_message = redact_secrets(validation_error)
    return RecolectaError(
        validation_error.error_type,
        (
            f"No se pudo validar la ruta remota {path_label}. "
            f"{safe_message}"
        ),
        retryable=validation_error.retryable,
    )


def _safe_remote_path_label(remote_path: str) -> str:
    """Render configured path context while redacting secrets and controls."""
    redacted = redact_secrets(remote_path)
    printable = "".join(
        character if character.isprintable() else " "
        for character in redacted
    )
    compact = " ".join(printable.split()) or "(vacía)"
    limit = 160
    if len(compact) > limit:
        compact = compact[: limit - 1] + "…"
    return repr(compact)


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
    if (
        isinstance(exc, ftplib.error_temp)
        and str(exc).lstrip().startswith("425")
    ):
        return RecolectaError(
            ErrorType.TCP_CONNECT,
            (
                "El servidor FTP no pudo abrir el canal de datos para listar "
                "la ruta. Confirme el modo pasivo y el rango de puertos de "
                "datos en el servidor y el firewall."
            ),
            retryable=True,
        )
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
        ErrorType.PARTIAL_TRANSFER: (
            "El servidor interrumpió el listado remoto o no pudo abrir el "
            "canal de datos. En FTP/FTPS, confirme el modo pasivo y que el "
            "firewall permita los puertos de datos."
        ),
        ErrorType.DISK_WRITE: (
            "No se pudo preparar o escribir el destino local configurado."
        ),
        ErrorType.PATH_INVALID: (
            "La ruta configurada o devuelta por el servidor no puede usarse "
            "de forma segura."
        ),
        ErrorType.UNKNOWN: (
            "La respuesta o el listado remoto no es compatible o no pudo "
            "interpretarse. Revise el protocolo y la codificación del listado."
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
