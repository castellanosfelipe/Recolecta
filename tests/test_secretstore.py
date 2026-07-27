import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.platform.secretstore import (
    SecretStoreError,
    create_secret_store,
    token_scheme,
)
from app.platform.secrets_fernet import FernetSecretStore


def test_fernet_round_trip_and_scheme_prefix() -> None:
    store = FernetSecretStore(Fernet.generate_key())
    token = store.encrypt("contraseña-ñ")
    assert token.startswith("fernet:")
    assert token_scheme(token) == "fernet"
    assert store.decrypt(token) == "contraseña-ñ"


def test_fernet_keyfile_survives_process_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HARVESTER_SECRET_KEY", raising=False)
    first = FernetSecretStore.from_environment(tmp_path)
    token = first.encrypt("persistente")
    second = FernetSecretStore.from_environment(tmp_path)
    assert second.decrypt(token) == "persistente"
    assert (tmp_path / ".secret.key").is_file()


def test_environment_key_does_not_create_keyfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("HARVESTER_SECRET_KEY", key)
    store = create_secret_store("dev", tmp_path)
    assert store.decrypt(store.encrypt("desde-env")) == "desde-env"
    assert not (tmp_path / ".secret.key").exists()


def test_wrong_fernet_key_has_actionable_error() -> None:
    token = FernetSecretStore(Fernet.generate_key()).encrypt("secreto")
    other = FernetSecretStore(Fernet.generate_key())
    with pytest.raises(SecretStoreError, match="reingrese la credencial"):
        other.decrypt(token)


def test_corrupted_fernet_token_has_actionable_error() -> None:
    store = FernetSecretStore(Fernet.generate_key())
    with pytest.raises(SecretStoreError, match="token está dañado"):
        store.decrypt("fernet:inválido")


def test_wrong_scheme_has_actionable_error() -> None:
    store = FernetSecretStore(Fernet.generate_key())
    with pytest.raises(SecretStoreError, match="requiere 'fernet'"):
        store.decrypt("dpapi:AAAA")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI requiere Windows")
def test_dpapi_user_round_trip(tmp_path: Path) -> None:
    from app.platform.secrets_dpapi import DpapiScope, DpapiSecretStore

    store = DpapiSecretStore(scope=DpapiScope.USER, data_dir=tmp_path)
    token = store.encrypt("credencial de usuario")
    assert token.startswith("dpapi:")
    assert store.decrypt(token) == "credencial de usuario"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI requiere Windows")
def test_dpapi_machine_round_trip_with_entropy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import app.platform.secrets_dpapi as module

    monkeypatch.setattr(module, "_load_machine_entropy", lambda path: b"x" * 32)
    store = module.DpapiSecretStore(
        scope=module.DpapiScope.MACHINE, data_dir=tmp_path
    )
    token = store.encrypt("credencial de máquina")
    assert token.startswith("dpapi-machine:")
    assert store.decrypt(token) == "credencial de máquina"
