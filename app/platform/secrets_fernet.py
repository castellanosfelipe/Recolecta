"""Fernet secret storage for development and CI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken

from app.platform.secretstore import SecretStoreError, wrong_scheme_message


class FernetSecretStore:
    """Encrypt credentials with a local key file or explicit environment key."""

    scheme: ClassVar[str] = "fernet"
    prefix: ClassVar[str] = "fernet:"

    def __init__(self, key: bytes | str) -> None:
        try:
            encoded = key.encode("ascii") if isinstance(key, str) else key
            self._fernet = Fernet(encoded)
        except (TypeError, UnicodeEncodeError, ValueError) as exc:
            raise SecretStoreError(
                "HARVESTER_SECRET_KEY no contiene una clave Fernet válida."
            ) from exc

    @classmethod
    def from_environment(cls, data_dir: Path) -> "FernetSecretStore":
        configured = os.environ.get("HARVESTER_SECRET_KEY", "").strip()
        if configured:
            return cls(configured)
        return cls(_read_or_create_key(data_dir / ".secret.key"))

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise SecretStoreError("No se puede cifrar una credencial vacía.")
        encrypted = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return self.prefix + encrypted

    def decrypt(self, token: str) -> str:
        if not token.startswith(self.prefix):
            raise SecretStoreError(wrong_scheme_message(token, self.scheme))
        try:
            payload = token.removeprefix(self.prefix).encode("ascii", errors="strict")
            return self._fernet.decrypt(payload).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
            raise SecretStoreError(
                "No fue posible descifrar la credencial Fernet. "
                "La clave pertenece a otro entorno o el token está dañado; "
                "reingrese la credencial."
            ) from exc


def _read_or_create_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        pass

    generated = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(generated)
        key_file.flush()
        os.fsync(key_file.fileno())
    return generated
