import ftplib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.connection_validation import (
    REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT,
    validate_connection_paths,
)
from app.errors import ErrorType, RecolectaError
from app.models import Connection, Protocol
from app.transports.base import RemoteFile


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
        self._last_listing_warnings: tuple[str, ...] = ()

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def close(self) -> None:
        self.closed = True

    @property
    def last_listing_warnings(self) -> tuple[str, ...]:
        return self._last_listing_warnings

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ):
        self.list_arguments = (remote_paths, recursive, max_depth)
        self.list_calls.append(self.list_arguments)
        self._last_listing_warnings = ("advertencia controlada",)
        root = remote_paths[0].rstrip("/\\")
        prefix = f"{root}/"
        return iter(
            remote_file
            for remote_file in self.files
            if remote_file.remote_path == root
            or remote_file.remote_path.startswith(prefix)
        )


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
    assert result.remote_files_found_is_exact is True
    assert (
        result.remote_files_sample_limit_per_root
        == REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT
    )
    assert result.warnings == ("advertencia controlada",)
    assert result.to_dict()["remote_files_found_is_exact"] is True
    assert transport.list_calls == [
        (("/entrada",), False, 0),
        (("/reportes",), False, 0),
    ]
    assert transport.closed is True
    assert factory_calls[0][1] == "credencial"
    assert not destination.exists()
    assert not list(tmp_path.rglob(".recolecta-validacion-*"))


@pytest.mark.parametrize(
    "protocol",
    (Protocol.FTP, Protocol.SFTP, Protocol.WEBDAV),
)
def test_validation_accepts_posix_server_root_with_remote_tree(
    tmp_path: Path,
    protocol: Protocol,
) -> None:
    destination = tmp_path / "destino"
    transport = FakeTransport(
        files=(RemoteFile("/entrada/reporte.csv", 10, None),)
    )

    result = validate_connection_paths(
        connection(
            destination,
            protocol=protocol,
            remote_paths=("/",),
            dest_template="{remote_tree}",
        ),
        secret=None,
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
        transport_factory=lambda *args, **kwargs: transport,
    )

    assert result.remote_paths == ("/",)
    assert result.remote_files_found == 1
    assert transport.list_calls == [(('/',), False, 0)]
    assert transport.closed is True


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


def test_validation_rejects_unimplemented_remote_post_actions(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()

    with pytest.raises(ValueError, match="acciones posteriores"):
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

    assert transport.connected is False
    assert transport.list_calls == []


def test_validation_samples_each_infinite_root_and_closes_every_iterator(
    tmp_path: Path,
) -> None:
    class InfiniteTransport:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.yielded: dict[str, int] = {}
            self.closed_iterators: list[str] = []
            self.events: list[str] = []
            self.closed = False
            self._last_listing_warnings: tuple[str, ...] = ()

        @property
        def last_listing_warnings(self) -> tuple[str, ...]:
            return self._last_listing_warnings

        def connect(self) -> None:
            self.events.append("transport-connected")

        def close(self) -> None:
            self.closed = True
            self.events.append("transport-closed")

        def iter_files(
            self,
            remote_paths: tuple[str, ...],
            *,
            recursive: bool,
            max_depth: int,
        ):
            assert recursive is False
            assert max_depth == 0
            root = remote_paths[0]
            self.calls.append(root)
            self.yielded[root] = 0
            self._last_listing_warnings = (
                f"advertencia de transporte para {root}",
            )

            def stream():
                try:
                    index = 0
                    while True:
                        index += 1
                        self.yielded[root] = index
                        yield RemoteFile(
                            f"{root.rstrip('/')}/archivo-{index}.bin",
                            index,
                            None,
                        )
                finally:
                    self.closed_iterators.append(root)
                    self.events.append(f"iterator-closed:{root}")

            return stream()

        def download_to(self, *args, **kwargs):
            raise AssertionError("La validación no debe descargar contenido.")

    transport = InfiniteTransport()

    result = validate_connection_paths(
        connection(
            tmp_path / "destino",
            remote_paths=("/entrada", "/reportes", "/procesados"),
        ),
        secret=None,
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
        transport_factory=lambda *args, **kwargs: transport,
    )

    sample_limit = REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT
    assert transport.calls == ["/entrada", "/reportes", "/procesados"]
    assert transport.yielded == {
        "/entrada": sample_limit + 1,
        "/reportes": sample_limit + 1,
        "/procesados": sample_limit + 1,
    }
    assert transport.closed_iterators == [
        "/entrada",
        "/reportes",
        "/procesados",
    ]
    assert transport.closed is True
    assert transport.events[-1] == "transport-closed"
    assert result.remote_files_found == sample_limit * 3
    assert result.remote_files_found_is_exact is False
    assert result.remote_files_sample_limit_per_root == sample_limit
    assert sum(
        "remote_files_found no representa el total exacto" in warning
        for warning in result.warnings
    ) == 3


def test_validation_reports_an_exact_sample_at_the_limit(
    tmp_path: Path,
) -> None:
    sample_limit = REMOTE_VALIDATION_SAMPLE_LIMIT_PER_ROOT
    files = tuple(
        RemoteFile(f"/entrada/archivo-{index}.bin", index, None)
        for index in range(sample_limit)
    )
    transport = FakeTransport(files=files)

    result = validate_connection_paths(
        connection(tmp_path / "destino"),
        secret=None,
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
        transport_factory=lambda *args, **kwargs: transport,
    )

    assert result.remote_files_found == sample_limit
    assert result.remote_files_found_is_exact is True
    assert not any("muestra de 100" in warning for warning in result.warnings)


def test_validation_closes_listing_iterator_before_transport_on_error(
    tmp_path: Path,
) -> None:
    class ExplodingTransport:
        def __init__(self) -> None:
            self.events: list[str] = []
            self._last_listing_warnings: tuple[str, ...] = ()

        @property
        def last_listing_warnings(self) -> tuple[str, ...]:
            return self._last_listing_warnings

        def connect(self) -> None:
            self.events.append("transport-connected")

        def close(self) -> None:
            self.events.append("transport-closed")

        def iter_files(self, *args, **kwargs):
            def stream():
                try:
                    yield RemoteFile("/entrada/primero.bin", 1, None)
                    raise RuntimeError("fallo durante listado incremental")
                finally:
                    self.events.append("iterator-closed")

            return stream()

    transport = ExplodingTransport()

    with pytest.raises(RecolectaError):
        validate_connection_paths(
            connection(tmp_path / "destino"),
            secret=None,
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
            transport_factory=lambda *args, **kwargs: transport,
        )

    assert transport.events == [
        "transport-connected",
        "iterator-closed",
        "transport-closed",
    ]
