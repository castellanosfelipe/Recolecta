"""Streaming hash support and disk-space/integrity checks."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO, Callable

from app.errors import ErrorType, RecolectaError
from app.models import VerifyMode


class StreamingVerifier:
    """Track bytes and optional SHA-256 without rereading a completed file."""

    def __init__(self, verify_mode: VerifyMode) -> None:
        self.verify_mode = VerifyMode(verify_mode)
        self.bytes_seen = 0
        self._hasher = (
            hashlib.sha256() if self.verify_mode == VerifyMode.SHA256 else None
        )

    def reset(self) -> None:
        self.bytes_seen = 0
        if self._hasher is not None:
            self._hasher = hashlib.sha256()

    def seed_from_partial(
        self,
        file: BinaryIO,
        *,
        length: int,
        block_size: int = 64 * 1024,
    ) -> None:
        """Seed hash state from an existing partial before network resume."""
        self.reset()
        if self._hasher is None:
            self.bytes_seen = length
            file.seek(length)
            return
        file.seek(0)
        remaining = length
        while remaining:
            chunk = file.read(min(block_size, remaining))
            if not chunk:
                raise RecolectaError(
                    ErrorType.INTEGRITY,
                    "El archivo parcial terminó antes del offset registrado.",
                )
            self.update(chunk)
            remaining -= len(chunk)
        file.seek(length)

    def update(self, chunk: bytes) -> None:
        self.bytes_seen += len(chunk)
        if self._hasher is not None:
            self._hasher.update(chunk)

    @property
    def sha256(self) -> str | None:
        return self._hasher.hexdigest() if self._hasher is not None else None

    def verify_size(self, *, actual: int, expected: int | None) -> None:
        if expected is not None and actual != expected:
            raise RecolectaError(
                ErrorType.INTEGRITY,
                f"El tamaño final es {actual} bytes; se esperaban {expected}.",
            )


def ensure_disk_space(
    destination_root: Path,
    planned_bytes: int,
    *,
    reserve_ratio: float = 0.10,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> None:
    """Abort before transfer unless free space covers data plus reserve."""
    if planned_bytes < 0:
        raise ValueError("planned_bytes no puede ser negativo.")
    if reserve_ratio < 0:
        raise ValueError("reserve_ratio no puede ser negativo.")
    probe = _nearest_existing_parent(destination_root)
    free = disk_usage(probe).free
    required = int(planned_bytes * (1.0 + reserve_ratio))
    if free < required:
        raise RecolectaError(
            ErrorType.DISK_SPACE,
            f"Espacio insuficiente: libres {free} bytes, requeridos {required}.",
        )


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise RecolectaError(
                ErrorType.DISK_SPACE,
                f"No fue posible localizar el volumen de destino para {path}.",
            )
        candidate = parent
    return candidate
