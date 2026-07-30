from pathlib import Path

from cryptography.fernet import Fernet

from app.connection_import import import_connections
from app.db import ConnectionRepository, Database
from app.platform.secrets_fernet import FernetSecretStore


def repository(tmp_path: Path) -> ConnectionRepository:
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    return ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )


def test_stability_backup_imports_files_and_reports_unsupported_sources(
    tmp_path: Path,
) -> None:
    connections = repository(tmp_path)
    backup = {
        "app": "StabilityMonitor",
        "version": "2.0.0",
        "connections": [
            {
                "name": "FTP sin secreto",
                "client": "Operaciones",
                "protocol": "FTP",
                "host": "ftp.example.test",
                "port": 21,
                "username": "reader",
                "targets_json": '["/entrada", "/salida"]',
                "timeout_s": 10,
                "retries": 2,
                "enabled": 1,
            },
            {
                "name": "SFTP con secreto",
                "protocol": "SFTP",
                "host": "sftp.example.test",
                "port": 22,
                "username": "reader",
                "targets_json": '["/reportes"]',
                "secret": " clave-local ",
                "enabled": 1,
            },
            {
                "name": "SFTP con llave local",
                "protocol": "SFTP",
                "host": "key.example.test",
                "auth_type": "key",
                "key_path": "C:/equipo-anterior/id_rsa",
                "targets_json": '["/llaves"]',
                "enabled": 1,
            },
            {
                "name": "Base SQL",
                "protocol": "SQLSERVER",
                "host": "db.example.test",
                "port": 1433,
            },
            {
                "name": "FTP inválido",
                "protocol": "FTP",
                "host": "bad.example.test",
                "port": 0,
                "targets_json": "[]",
            },
            {
                "name": 123,
                "protocol": "FTP",
                "host": "typed.example.test",
                "targets_json": "[]",
            },
            {
                "name": "Puerto infinito",
                "protocol": "FTP",
                "host": "infinite.example.test",
                "port": float("inf"),
                "targets_json": "[]",
            },
            {
                "name": "WebDAV posterior",
                "protocol": "WEBDAVS",
                "host": "dav.example.test",
                "targets_json": '["/posterior"]',
            },
        ],
    }

    result = import_connections(backup, connections)

    assert result.total == 8
    assert len(result.created) == 4
    assert len(result.skipped) == 1
    assert len(result.errors) == 3
    assert result.skipped[0].protocol == "SQLSERVER"
    assert "puerto" in result.errors[0].reason.lower()
    assert result.errors[1].name == "123"
    assert result.errors[1].reason == "El campo name debe ser texto."
    assert result.errors[2].name == "Puerto infinito"
    assert "port" in result.errors[2].reason.lower()
    by_name = {item.name: item for item in connections.list()}
    paused = by_name["FTP sin secreto"]
    with_secret = by_name["SFTP con secreto"]
    key_based = by_name["SFTP con llave local"]
    later = by_name["WebDAV posterior"]
    assert paused.name == "FTP sin secreto"
    assert paused.remote_paths == ("/entrada", "/salida")
    assert paused.enabled is False
    assert paused.full_local_reconciliation is False
    assert paused.has_secret is False
    assert with_secret.name == "SFTP con secreto"
    assert with_secret.enabled is False
    assert with_secret.has_secret is True
    assert connections.get_secret(with_secret.id) == " clave-local "
    assert key_based.auth_type.value == "key"
    assert key_based.enabled is False
    assert key_based.has_secret is False
    assert later.name == "WebDAV posterior"


def test_import_is_idempotent_and_recolecta_schedule_round_trips(
    tmp_path: Path,
) -> None:
    connections = repository(tmp_path)
    backup = {
        "app": "Recolecta",
        "version": "0.1.0",
        "connections": [
            {
                "name": "Cierre diario",
                "protocol": "WEBDAVS",
                "host": "dav.example.test",
                "port": 443,
                "username": "reader",
                "remote_paths": ["/cierres"],
                "schedule_time": "23:45",
                "timezone": "America/Bogota",
                "full_local_reconciliation": "true",
                "enabled": True,
            },
            {
                "name": "Timeout no finito",
                "protocol": "FTP",
                "host": "nan.example.test",
                "timeout_s": float("nan"),
            },
            {
                "name": "Reintentos decimales",
                "protocol": "FTP",
                "host": "fraction.example.test",
                "retries": 2.5,
            },
            {
                "name": "Booleano ambiguo",
                "protocol": "FTP",
                "host": "bool.example.test",
                "enabled": 2,
            },
            {
                "name": "Rutas no textuales",
                "protocol": "FTP",
                "host": "paths.example.test",
                "remote_paths": [None, 7, {"path": "/entrada"}],
            },
        ],
    }

    first = import_connections(backup, connections)
    second = import_connections(backup, connections)

    assert first.to_dict()["created_count"] == 1
    assert first.to_dict()["error_count"] == 4
    assert first.created[0].schedule_time == "23:45"
    assert first.created[0].full_local_reconciliation is True
    assert first.created[0].dest_template == r"{remote_tree}"
    assert first.created[0].enabled is False
    assert second.to_dict()["created_count"] == 0
    assert second.to_dict()["skipped_count"] == 1
    assert second.to_dict()["error_count"] == 4
    assert second.skipped[0].reason == "La misma conexión ya existe."


def test_import_rejects_unrelated_or_malformed_backups(tmp_path: Path) -> None:
    connections = repository(tmp_path)

    for backup in (
        {"app": "OtraApp", "connections": []},
        {"app": "StabilityMonitor", "connections": "no-lista"},
    ):
        try:
            import_connections(backup, connections)
        except ValueError as exc:
            assert str(exc)
        else:
            raise AssertionError("El backup inválido debía rechazarse.")
