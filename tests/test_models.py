from dataclasses import replace

import pytest

from app.api.schemas import ConnectionCreate, ConnectionPatch
from app.models import (
    AuthType,
    Connection,
    PostAction,
    Protocol,
    WindowMode,
)


def valid_connection(**changes) -> Connection:
    base = Connection(
        name=" SFTP Producción ",
        client=" Cliente A ",
        protocol=Protocol.SFTP,
        host=" 10.0.0.10 ",
        username=" monitor ",
        remote_paths=(" /entrada ", ""),
        dest_root=r"D:\Descargas",
    )
    return replace(base, **changes)


def test_normalization_applies_protocol_port_and_trims_values() -> None:
    connection = valid_connection(
        port=None,
        schedule_time=" 5:07 ",
    ).normalized()
    assert connection.name == "SFTP Producción"
    assert connection.client == "Cliente A"
    assert connection.host == "10.0.0.10"
    assert connection.port == 22
    assert connection.remote_paths == ("/entrada",)
    assert connection.schedule_time == "05:07"


@pytest.mark.parametrize(
    ("protocol", "port"),
    [
        (Protocol.FTP, 21),
        (Protocol.FTPS, 21),
        (Protocol.SFTP, 22),
        (Protocol.WEBDAV, 80),
        (Protocol.WEBDAVS, 443),
        (Protocol.SMB, 445),
    ],
)
def test_protocol_default_ports(protocol: Protocol, port: int) -> None:
    assert valid_connection(protocol=protocol, port=None).normalized().port == port


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": ""}, "nombre"),
        ({"host": ""}, "host"),
        ({"port": 70000}, "puerto"),
        ({"timezone": "Mars/Olympus"}, "Zona horaria"),
        ({"schedule_time": "25:10"}, "hora de la conexión"),
        ({"schedule_time": "mañana"}, "formato HH:MM"),
        ({"min_size_bytes": 10, "max_size_bytes": 5}, "mínimo"),
        ({"max_parallel_files": 0}, "trabajador"),
        ({"bandwidth_limit_kbps": 0}, "ancho de banda"),
        ({"auth_type": AuthType.KEY, "key_path": None}, "ruta de llave"),
        ({"post_action": PostAction.MOVE_REMOTE}, "ruta de destino remota"),
    ],
)
def test_invalid_connections_have_actionable_messages(changes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        valid_connection(**changes).normalized()


def test_with_changes_blocks_internal_fields() -> None:
    connection = valid_connection().normalized()
    with pytest.raises(ValueError, match="no editables"):
        connection.with_changes({"has_secret": True})


def test_public_serialization_contains_no_secret_material() -> None:
    connection = replace(
        valid_connection().normalized(),
        id=7,
        has_secret=True,
        full_local_reconciliation=True,
    )
    public = connection.to_public_dict()
    assert public["has_secret"] is True
    assert "secret" not in public
    assert "secret_encrypted" not in public
    assert public["protocol"] == "SFTP"
    assert public["window_mode"] == WindowMode.CALENDAR_DAY.value
    assert public["full_local_reconciliation"] is True


def test_full_local_reconciliation_is_mutable_and_defaults_to_remote_tree() -> None:
    connection = valid_connection().normalized()

    updated = connection.with_changes({"full_local_reconciliation": True})

    assert connection.full_local_reconciliation is False
    assert connection.dest_template == r"{remote_tree}"
    assert updated.full_local_reconciliation is True
    assert ConnectionCreate(name="Nueva", host="example.test").model_dump()[
        "dest_template"
    ] == r"{remote_tree}"
    assert ConnectionCreate(name="Nueva", host="example.test").model_dump()[
        "full_local_reconciliation"
    ] is False
    assert ConnectionPatch(
        full_local_reconciliation=True
    ).model_dump(exclude_unset=True) == {
        "full_local_reconciliation": True
    }


@pytest.mark.parametrize(
    "template",
    (
        r"{run_id}\{remote_tree}",
        r"{run_id!s}\{remote_tree}",
        r"{run_id:05d}\{remote_tree}",
        r"{filename:{run_id}}\{remote_tree}",
    ),
)
def test_full_local_reconciliation_rejects_run_specific_destination(
    template: str,
) -> None:
    with pytest.raises(ValueError, match="no admite \\{run_id\\}"):
        replace(
            valid_connection(),
            full_local_reconciliation=True,
            dest_template=template,
        ).normalized()
