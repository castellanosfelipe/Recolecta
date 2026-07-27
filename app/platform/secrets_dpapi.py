"""Windows DPAPI secret storage for user and machine execution scopes."""

from __future__ import annotations

import base64
import os
import secrets
import sys
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from app.platform.secretstore import SecretStoreError, wrong_scheme_message


CRYPTPROTECT_LOCAL_MACHINE = 0x4


class DpapiScope(StrEnum):
    USER = "user"
    MACHINE = "machine"


class DpapiSecretStore:
    """Protect credentials using the current Windows identity or local machine."""

    description: ClassVar[str] = "FileHarvester credential"

    def __init__(self, *, scope: DpapiScope, data_dir: Path) -> None:
        if sys.platform != "win32":
            raise SecretStoreError("DPAPI solo está disponible en Windows.")
        self.scope = DpapiScope(scope)
        self.scheme = "dpapi-machine" if self.scope == DpapiScope.MACHINE else "dpapi"
        self.prefix = self.scheme + ":"
        self._flags = (
            CRYPTPROTECT_LOCAL_MACHINE
            if self.scope == DpapiScope.MACHINE
            else 0
        )
        self._entropy = (
            _load_machine_entropy(data_dir / ".entropy")
            if self.scope == DpapiScope.MACHINE
            else None
        )

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise SecretStoreError("No se puede cifrar una credencial vacía.")
        try:
            import win32crypt

            protected = win32crypt.CryptProtectData(
                plaintext.encode("utf-8"),
                self.description,
                self._entropy,
                None,
                None,
                self._flags,
            )
        except Exception as exc:
            raise SecretStoreError(
                "Windows no pudo cifrar la credencial con DPAPI."
            ) from exc
        return self.prefix + base64.urlsafe_b64encode(protected).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token.startswith(self.prefix):
            raise SecretStoreError(wrong_scheme_message(token, self.scheme))
        try:
            payload = base64.b64decode(
                token.removeprefix(self.prefix).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            import win32crypt

            _, plaintext = win32crypt.CryptUnprotectData(
                payload,
                self._entropy,
                None,
                None,
                self._flags,
            )
            return plaintext.decode("utf-8")
        except Exception as exc:
            scope_label = "máquina" if self.scope == DpapiScope.MACHINE else "usuario"
            raise SecretStoreError(
                f"No fue posible descifrar la credencial DPAPI de {scope_label}. "
                "La base pertenece a otro equipo o cuenta, o el token está dañado; "
                "reingrese la credencial."
            ) from exc


def _load_machine_entropy(path: Path) -> bytes:
    """Create additional machine entropy once and enforce a restricted DACL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        entropy = path.read_bytes()
    except FileNotFoundError:
        generated = secrets.token_bytes(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            entropy = path.read_bytes()
        else:
            with os.fdopen(descriptor, "wb") as entropy_file:
                entropy_file.write(generated)
                entropy_file.flush()
                os.fsync(entropy_file.fileno())
            entropy = generated
    if len(entropy) < 32:
        raise SecretStoreError(
            "El archivo data/.entropy está dañado; restaure el respaldo o "
            "reingrese las credenciales."
        )
    _restrict_entropy_acl(path)
    return entropy


def _restrict_entropy_acl(path: Path) -> None:
    """Allow only SYSTEM and Administrators to read the entropy file."""
    try:
        import ntsecuritycon
        import win32security

        system_sid = win32security.CreateWellKnownSid(
            win32security.WinLocalSystemSid, None
        )
        administrators_sid = win32security.CreateWellKnownSid(
            win32security.WinBuiltinAdministratorsSid, None
        )
        dacl = win32security.ACL()
        access = (
            ntsecuritycon.FILE_GENERIC_READ
            | ntsecuritycon.FILE_GENERIC_WRITE
            | ntsecuritycon.DELETE
        )
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, access, system_sid
        )
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, access, administrators_sid
        )
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        win32security.SetFileSecurity(
            str(path),
            win32security.DACL_SECURITY_INFORMATION,
            descriptor,
        )
    except Exception as exc:
        raise SecretStoreError(
            "No fue posible restringir la ACL de data/.entropy a SYSTEM y "
            "Administrators. Ejecute la instalación de servicio como administrador."
        ) from exc
