"""Secret-store abstraction and runtime-specific factory."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from app.errors import ErrorType, RecolectaError


class SecretStoreError(RecolectaError):
    """Actionable failure to encrypt or decrypt local credentials."""

    def __init__(self, message: str) -> None:
        super().__init__(ErrorType.AUTH, message, retryable=False)


@runtime_checkable
class SecretStore(Protocol):
    """Minimal interface used by persistence and transports."""

    scheme: str

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext into a scheme-prefixed token."""

    def decrypt(self, token: str) -> str:
        """Decrypt a token or raise an actionable Spanish error."""


def token_scheme(token: str) -> str:
    """Return the prefix before the first colon, or an empty string."""
    return token.partition(":")[0].lower() if ":" in token else ""


def wrong_scheme_message(token: str, expected: str) -> str:
    actual = token_scheme(token) or "desconocido"
    return (
        f"El secreto usa el esquema '{actual}', pero este proceso requiere "
        f"'{expected}'. Reingrese la credencial en este equipo y modo de ejecución."
    )


def create_secret_store(mode: str, data_dir: Path) -> SecretStore:
    """Build the required store for dev, interactive Windows, or service mode."""
    normalized = mode.strip().lower()
    if normalized == "dev":
        from app.platform.secrets_fernet import FernetSecretStore

        return FernetSecretStore.from_environment(data_dir)
    if normalized in {"windows", "service"}:
        from app.platform.secrets_dpapi import DpapiScope, DpapiSecretStore

        scope = DpapiScope.MACHINE if normalized == "service" else DpapiScope.USER
        return DpapiSecretStore(scope=scope, data_dir=data_dir)
    raise ValueError("El modo de secretos debe ser dev, windows o service.")
