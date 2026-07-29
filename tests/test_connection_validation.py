import ftplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connection_validation import validate_connection_paths
from app.errors import ErrorType, RecolectaError
from app.models import Connection, Protocol
from app.transports.base import ListingResult, RemoteFile


class FakeTransport:
    def __init__(
        self,
        *,
        files: tuple[RemoteFile, ...] = (),
        connect_error: Exception | None = None,
    ) -> None:
        self.files = files
        self.connect_error = connect_error
        self.connected = False
        self.closed = False
        self.list_arguments: tuple[tuple[str, ...], bool, int] | None = None
        self.list_calls: list[tuple[tuple[str, ...], bool, int]] = []

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        self.list_arguments = (remote_paths, recursive, max_depth)
        self.list_calls.append(self.list_arguments)
        return ListingResult(self.files, ("advertencia controlada",))


def connection(destination: Path, **changes) -> Connection:
    values = {
        "name": "Origen",
        "protocol": Protocol.FTP,
        "host": "ftp.example.test",
        "remote_paths": ("/entrada", "/reportes"),
        "dest_root": str(destination),
    }
    values.update(changes)
    return Connection(**values)


def test_validation_checks_all_remote_roots_and_local_write_access(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "nuevo" / "destino"
    remote = RemoteFile(
        "/entrada/reporte.csv",
        10,
        datetime(2026, 7, 29, tzinfo=timezone.utc),
    )
    transport = FakeTransport(files=(remote,))
    factory_calls = []

    def factory(draft, *, secret, known_hosts):
        factory_calls.append((draft, secret, known_hosts))
        return transport

    result = validate_connection_paths(
        connection(destination),
        secret="credencial",
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
        transport_factory=factory,
    )

    assert result.local_path == str(destination.resolve())
    assert result.remote_paths == ("/entrada", "/reportes")
    assert result.remote_files_found == 1
    assert result.warnings == ("advertencia controlada",)
    assert transport.list_arguments == (
        ("/entrada", "/reportes"),
        False,
        0,
    )
    assert transport.closed is True
    assert factory_calls[0][1] == "credencial"
    assert not destination.exists()
    assert not list(tmp_path.rglob(".recolecta-validacion-*"))


def test_validation_rejects_empty_remote_paths_before_connecting(
    tmp_path: Path,
) -> None:
    called = False

    def factory(*args, **kwargs):
        nonlocal called
        called = True
        return FakeTransport()

    with pytest.raises(ValueError, match="al menos una ruta remota"):
        validate_connection_paths(
            connection(tmp_path / "destino", remote_paths=()),
            secret=None,
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
            transport_factory=factory,
        )

    assert called is False


def test_validation_reports_authentication_failure_and_closes_transport(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        connect_error=ftplib.error_perm("530 password=credencial-real")
    )

    with pytest.raises(RecolectaError) as captured:
        validate_connection_paths(
            connection(tmp_path / "destino"),
            secret="credencial-real",
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
            transport_factory=lambda *args, **kwargs: transport,
        )

    assert captured.value.error_type == ErrorType.AUTH
    assert "credencial fue rechazada" in str(captured.value).lower()
    assert "credencial-real" not in str(captured.value)
    assert transport.closed is True


def test_validation_rejects_a_local_file_as_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "archivo.txt"
    destination.write_text("no es una carpeta", encoding="utf-8")

    with pytest.raises(RecolectaError) as captured:
        validate_connection_paths(
            connection(destination),
            secret=None,
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
            transport_factory=lambda *args, **kwargs: FakeTransport(),
        )

    assert captured.value.error_type == ErrorType.DISK_WRITE
    assert "no es una carpeta" in str(captured.value)
    assert destination.read_text(encoding="utf-8") == "no es una carpeta"


def test_validation_rejects_an_invalid_destination_template(
    tmp_path: Path,
) -> None:
    with pytest.raises(RecolectaError) as captured:
        validate_connection_paths(
            connection(
                tmp_path / "destino",
                dest_template="{token_inexistente}",
            ),
            secret=None,
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
            transport_factory=lambda *args, **kwargs: FakeTransport(),
        )

    assert captured.value.error_type == ErrorType.PATH_INVALID
    assert "token no válido" in str(captured.value)


def test_validation_checks_a_distinct_remote_move_destination(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()

    validate_connection_paths(
        connection(
            tmp_path / "destino",
            post_action="move_remote",
            post_action_path="/procesados",
        ),
        secret=None,
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
        transport_factory=lambda *args, **kwargs: transport,
    )

    assert transport.list_calls == [
        (("/entrada", "/reportes"), False, 0),
        (("/procesados",), False, 0),
    ]
