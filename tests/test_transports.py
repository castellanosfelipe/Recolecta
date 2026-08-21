import ftplib
import gzip
import os
import ssl
import stat
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import paramiko
import pytest

from app.errors import ErrorType, RecolectaError
from app.models import Connection, Protocol
from app.transports import create_transport
from app.transports.ftp import FtpTransport
from app.transports.sftp import SftpTransport
from app.transports.smb import SmbTransport
from app.transports.webdav import WebDavTransport


def connection(protocol: Protocol, **changes) -> Connection:
    base = Connection(
        name=f"Prueba {protocol}",
        protocol=protocol,
        host="example.test",
        username="operator",
        remote_paths=("/root",),
        recursive=True,
        max_depth=3,
        dest_root="downloads",
    )
    return replace(base, **changes).normalized()


class FakeFtp:
    def __init__(self, *, mlsd_supported: bool = True) -> None:
        self.mlsd_supported = mlsd_supported

    def mlsd(self, path, facts):
        if not self.mlsd_supported:
            from ftplib import error_perm

            raise error_perm("500 MLSD not understood")
        return iter(
            [
                (
                    "report.csv",
                    {
                        "type": "file",
                        "size": "12",
                        "modify": "20260101010101",
                    },
                )
            ]
        )

    def sendcmd(self, command):
        return "213 20260726030405"

    def voidcmd(self, command):
        return "200 Type set to: Binary."

    def size(self, path):
        return 12

    def retrlines(self, command, callback):
        callback("-rw-r--r-- 1 owner group 42 Jul 26 03:04 legacy.csv")


@pytest.mark.parametrize(
    ("ssl_mode", "verify_mode", "check_hostname"),
    (
        ("required", ssl.CERT_REQUIRED, True),
        ("insecure", ssl.CERT_NONE, False),
    ),
)
def test_ftps_certificate_verification_is_explicit(
    monkeypatch,
    ssl_mode: str,
    verify_mode: ssl.VerifyMode,
    check_hostname: bool,
) -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.check_hostname = True
            self.verify_mode = ssl.CERT_REQUIRED

    class FakeTls:
        def __init__(self, *, context) -> None:
            self.context = context

        def connect(self, host, port, timeout):
            return None

        def login(self, username, secret):
            return None

        def set_pasv(self, passive):
            return None

        def prot_p(self):
            return None

        def quit(self):
            return None

    context = FakeContext()
    monkeypatch.setattr(ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(ftplib, "FTP_TLS", FakeTls)
    transport = FtpTransport(
        connection(Protocol.FTPS, ssl_mode=ssl_mode),
        secret="x",
    )

    transport.connect()
    transport.close()

    assert context.verify_mode == verify_mode
    assert context.check_hostname is check_hostname


def test_ftp_utf8_negotiation_is_best_effort(monkeypatch) -> None:
    class LegacyFtp:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.passive: bool | None = None

        def connect(self, host, port, timeout):
            return None

        def login(self, username, secret):
            return None

        def sendcmd(self, command):
            self.commands.append(command)
            raise ftplib.error_perm("500 OPTS UTF8 not understood")

        def set_pasv(self, passive):
            self.passive = passive

        def quit(self):
            return None

    client = LegacyFtp()
    monkeypatch.setattr(ftplib, "FTP", lambda: client)
    transport = FtpTransport(connection(Protocol.FTP), secret="x")

    transport.connect()
    transport.close()

    assert client.commands == ["OPTS UTF8 ON"]
    assert client.encoding == "utf-8"
    assert client.passive is True


def test_ftp_utf8_negotiation_propagates_transient_failure(monkeypatch) -> None:
    class TemporarilyUnavailableFtp:
        def __init__(self) -> None:
            self.closed = False

        def connect(self, host, port, timeout):
            return None

        def login(self, username, secret):
            return None

        def sendcmd(self, command):
            raise ftplib.error_temp(
                "421 Service not available, closing control connection"
            )

        def quit(self):
            self.closed = True

    client = TemporarilyUnavailableFtp()
    monkeypatch.setattr(ftplib, "FTP", lambda: client)
    transport = FtpTransport(connection(Protocol.FTP), secret="x")

    with pytest.raises(ftplib.error_temp, match="421"):
        transport.connect()

    assert client.closed is True
    assert transport._ftp is None


def test_ftp_uses_streamed_mlsd_modify_metadata() -> None:
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=FakeFtp(),
    )
    with transport:
        result = transport.list_files(("/root",), recursive=False, max_depth=0)
    assert result.warnings == ()
    assert result.files[0].timestamp_source == "MLSD"
    assert result.files[0].mtime_utc == datetime(
        2026, 1, 1, 1, 1, 1, tzinfo=timezone.utc
    )


def test_ftp_directory_only_root_finds_nested_files_only_with_recursion() -> None:
    class NestedOnlyFtp(FakeFtp):
        def mlsd(self, path, facts):
            if path == "/entrada":
                return iter([("lote-01", {"type": "dir"})])
            if path == "/entrada/lote-01":
                return iter(
                    [
                        (
                            "documento.pdf",
                            {
                                "type": "file",
                                "size": "12",
                                "modify": "20260814115121",
                            },
                        )
                    ]
                )
            raise AssertionError(f"Ruta FTP inesperada: {path}")

    transport = FtpTransport(
        connection(Protocol.FTP, remote_paths=("/entrada",)),
        secret="x",
        client=NestedOnlyFtp(),
    )

    root_only = transport.list_files(
        ("/entrada",),
        recursive=False,
        max_depth=3,
    )
    recursive = transport.list_files(
        ("/entrada",),
        recursive=True,
        max_depth=1,
    )

    assert root_only.files == ()
    assert [item.remote_path for item in recursive.files] == [
        "/entrada/lote-01/documento.pdf"
    ]


def test_ftp_negotiates_mlsd_facts_once_for_recursive_session() -> None:
    class RecursiveFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__()
            self.commands: list[str] = []

        def sendcmd(self, command):
            self.commands.append(command)
            return "200 OK"

        def mlsd(self, path, facts):
            if path == "/entrada":
                return iter(
                    [
                        ("uno", {"type": "dir"}),
                        ("dos", {"type": "dir"}),
                    ]
                )
            return iter(
                [
                    (
                        "documento.pdf",
                        {
                            "type": "file",
                            "size": "12",
                            "modify": "20260814115121",
                        },
                    )
                ]
            )

    client = RecursiveFtp()
    transport = FtpTransport(
        connection(Protocol.FTP, remote_paths=("/entrada",)),
        secret="x",
        client=client,
    )

    result = transport.list_files(
        ("/entrada",),
        recursive=True,
        max_depth=1,
    )

    assert len(result.files) == 2
    assert client.commands.count("OPTS MLST type;size;modify;") == 1


def test_ftp_caches_list_fallback_for_recursive_session() -> None:
    class ListOnlyTreeFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__(mlsd_supported=False)
            self.mlsd_calls = 0
            self.commands: list[str] = []
            self.list_commands: list[str] = []

        def sendcmd(self, command):
            self.commands.append(command)
            return "200 OK"

        def mlsd(self, path, facts):
            self.mlsd_calls += 1
            raise ftplib.error_perm("500 MLSD not understood")

        def retrlines(self, command, callback):
            self.list_commands.append(command)
            if command == "LIST /entrada":
                callback("drwxr-xr-x 1 owner group 0 Aug 14 2026 uno")
                callback("drwxr-xr-x 1 owner group 0 Aug 14 2026 dos")
                return
            callback(
                "-rw-r--r-- 1 owner group 12 Aug 14 2026 documento.pdf"
            )

    client = ListOnlyTreeFtp()
    transport = FtpTransport(
        connection(Protocol.FTP, remote_paths=("/entrada",)),
        secret="x",
        client=client,
    )

    result = transport.list_files(
        ("/entrada",),
        recursive=True,
        max_depth=1,
    )

    assert len(result.files) == 2
    assert client.mlsd_calls == 1
    assert client.commands.count("OPTS MLST type;size;modify;") == 1
    assert client.list_commands == [
        "LIST /entrada",
        "LIST /entrada/uno",
        "LIST /entrada/dos",
    ]


def test_ftp_wide_directory_emits_heartbeat_during_listing() -> None:
    entries_consumed = 0

    class WideFtp(FakeFtp):
        def mlsd(self, path, facts):
            def entries():
                nonlocal entries_consumed
                for index in range(1_000):
                    entries_consumed += 1
                    yield f"carpeta-{index:04d}", {"type": "dir"}

            return entries()

    class StopListing(Exception):
        pass

    callbacks: list[tuple[str, int, bool, int]] = []

    def observe(
        path: str,
        depth: int,
        count_location: bool,
        entries_delta: int,
    ) -> None:
        callbacks.append((path, depth, count_location, entries_delta))
        if not count_location:
            raise StopListing

    transport = FtpTransport(
        connection(Protocol.FTP, remote_paths=("/entrada",)),
        secret="x",
        client=WideFtp(),
    )
    transport.set_listing_progress_callback(observe)

    with pytest.raises(StopListing):
        transport.list_files(
            ("/entrada",),
            recursive=True,
            max_depth=1,
        )

    assert callbacks == [
        ("/entrada", 0, True, 0),
        ("/entrada", 0, False, 100),
    ]
    assert entries_consumed == 100


def test_ftp_iter_files_consumes_mlsd_lazily() -> None:
    consumed: list[str] = []

    class LazyFtp(FakeFtp):
        def mlsd(self, path, facts):
            def entries():
                for name in ("first.csv", "second.csv"):
                    consumed.append(name)
                    yield (
                        name,
                        {
                            "type": "file",
                            "size": "12",
                            "modify": "20260101010101",
                        },
                    )

            return entries()

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=LazyFtp(),
    )
    with transport:
        files = transport.iter_files(
            ("/root",),
            recursive=False,
            max_depth=0,
        )
        assert next(files).name == "first.csv"
        assert consumed == ["first.csv"]
        assert transport.last_listing_warnings == ()
        files.close()


class _StreamingFtpLines:
    def __init__(self, lines: tuple[bytes, ...]) -> None:
        self._lines = iter(lines)
        self.lines_yielded = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        line = next(self._lines)
        self.lines_yielded += 1
        return line

    def close(self) -> None:
        self.closed = True


class _FtpListingSocket:
    def __init__(self) -> None:
        self.closed = False
        self.stream = _StreamingFtpLines(
            (
                b"type=file;size=1;modify=20260101010101; first.csv\r\n",
                b"type=file;size=1;modify=20260101010101; second.csv\r\n",
            )
        )

    def makefile(self, mode):
        assert mode == "rb"
        return self.stream

    def close(self) -> None:
        self.closed = True


class _FtpListingWithAbortReply(FakeFtp):
    encoding = "utf-8"

    def __init__(self) -> None:
        super().__init__()
        self.data_socket = _FtpListingSocket()
        self.voidresp_calls = 0

    def transfercmd(self, command):
        assert command == "MLSD /root"
        return self.data_socket

    def voidresp(self):
        self.voidresp_calls += 1
        raise ftplib.error_temp("426 Transfer aborted")


class _BytesDataSocket:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def makefile(self, mode):
        assert mode == "rb"
        return BytesIO(self.payload)

    def close(self) -> None:
        self.closed = True


def test_ftp_rejects_malformed_streamed_mlsd_instead_of_omitting_it() -> None:
    class MalformedMlsdFtp(FakeFtp):
        encoding = "utf-8"

        def transfercmd(self, command):
            assert command == "MLSD /root"
            return _BytesDataSocket(b"respuesta-no-mlsd dato-sensible\r\n")

        def voidresp(self):
            return "226 Transfer complete"

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=MalformedMlsdFtp(),
    )

    with pytest.raises(RecolectaError) as captured:
        transport.list_files(("/root",), recursive=False, max_depth=0)

    assert captured.value.error_type == ErrorType.PROTOCOL
    assert "MLSD" in str(captured.value)
    assert "dato-sensible" not in str(captured.value)


def test_ftp_partial_listing_ignores_expected_426_abort_reply() -> None:
    client = _FtpListingWithAbortReply()
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=client,
    )

    with transport:
        files = transport.iter_files(
            ("/root",),
            recursive=False,
            max_depth=0,
        )
        assert next(files).name == "first.csv"
        files.close()

    assert client.data_socket.closed is True
    assert client.data_socket.stream.closed is True
    assert client.data_socket.stream.lines_yielded == 1
    assert client.voidresp_calls == 1


def test_ftp_completed_listing_propagates_426_reply() -> None:
    client = _FtpListingWithAbortReply()
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=client,
    )

    with transport, pytest.raises(ftplib.error_temp, match="426"):
        list(
            transport.iter_files(
                ("/root",),
                recursive=False,
                max_depth=0,
            )
        )

    assert client.data_socket.closed is True
    assert client.voidresp_calls == 1


def test_ftp_attempts_mlsd_when_opts_mlst_is_rejected() -> None:
    class OptsRejectedFtp(FakeFtp):
        encoding = "utf-8"

        def __init__(self) -> None:
            super().__init__()
            self.commands: list[str] = []

        def sendcmd(self, command):
            self.commands.append(command)
            if command.startswith("OPTS MLST"):
                raise ftplib.error_perm("501 OPTS MLST not understood")
            return "200 OK"

        def transfercmd(self, command):
            self.commands.append(command)
            assert command == "MLSD /root"
            return _BytesDataSocket(
                b"type=file;size=1;modify=20260101010101; report.csv\r\n"
            )

        def voidresp(self):
            return "226 Transfer complete"

    client = OptsRejectedFtp()
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=client,
    )

    result = transport.list_files(("/root",), recursive=False, max_depth=0)

    assert [file.name for file in result.files] == ["report.csv"]
    assert result.warnings == ()
    assert client.commands == [
        "OPTS MLST type;size;modify;",
        "MLSD /root",
    ]


def test_ftp_opts_mlst_propagates_transient_failure() -> None:
    class TemporarilyUnavailableFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__()
            self.mlsd_calls = 0

        def sendcmd(self, command):
            raise ftplib.error_temp(
                "421 Service not available, closing control connection"
            )

        def mlsd(self, path, facts):
            self.mlsd_calls += 1
            return super().mlsd(path, facts)

    client = TemporarilyUnavailableFtp()
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=client,
    )

    with pytest.raises(ftplib.error_temp, match="421"):
        transport.list_files(("/root",), recursive=False, max_depth=0)

    assert client.mlsd_calls == 0


def test_ftp_ansi_hint_reaches_a_separate_worker_without_retrying_retr(
    monkeypatch,
) -> None:
    payload = b"\x00\xd1\xff\r\n"

    class LegacyAnsiIisListingFtp:
        encoding = "utf-8"

        def __init__(self) -> None:
            self.commands: list[bytes] = []

        def sendcmd(self, command):
            self.commands.append(command.encode(self.encoding))
            raise ftplib.error_perm("500 OPTS MLST not understood")

        def voidcmd(self, command):
            return "200 Type set"

        def transfercmd(self, command):
            encoded = command.encode(self.encoding)
            self.commands.append(encoded)
            if command.startswith("MLSD "):
                raise ftplib.error_perm("500 MLSD not understood")
            if command == "LIST /ORIGEN_DOCUMENTOS":
                return _BytesDataSocket(
                    b"08-13-26  09:17AM       <DIR>          "
                    b"CARPETA NI\xd1O\r\n"
                )
            assert command == (
                "LIST /ORIGEN_DOCUMENTOS/CARPETA NIÑO"
            )
            return _BytesDataSocket(
                b"08-13-26  09:18AM                    5 documento.bin\r\n"
            )

        def voidresp(self):
            return "226 Transfer complete"

    class LegacyAnsiIisDownloadFtp:
        encoding = "utf-8"

        def __init__(self) -> None:
            self.commands: list[bytes] = []
            self.retr_calls = 0

        def connect(self, host, port, timeout):
            return None

        def login(self, username, secret):
            return None

        def sendcmd(self, command):
            self.commands.append(command.encode(self.encoding))
            return "200 OK"

        def set_pasv(self, passive):
            return None

        def quit(self):
            return None

        def retrbinary(
            self,
            command,
            callback,
            blocksize=8192,
            rest=None,
        ):
            self.retr_calls += 1
            self.commands.append(command.encode(self.encoding))
            callback(payload)
            return "226 Transfer complete"

    listing_client = LegacyAnsiIisListingFtp()
    configured = connection(
        Protocol.FTP,
        remote_paths=("/ORIGEN_DOCUMENTOS",),
        recursive=True,
        max_depth=1,
    )
    listing_transport = FtpTransport(
        configured,
        secret="x",
        client=listing_client,
    )

    result = listing_transport.list_files(
        configured.remote_paths,
        recursive=True,
        max_depth=1,
    )
    download_client = LegacyAnsiIisDownloadFtp()
    monkeypatch.setattr(ftplib, "FTP", lambda: download_client)
    worker_transport = FtpTransport(
        configured,
        secret="x",
        command_encoding=listing_transport.command_encoding,
    )
    worker_transport.connect()
    target = BytesIO()
    transfer = worker_transport.download_to(
        result.files[0].remote_path,
        target,
        offset=0,
        block_size=1024,
        on_chunk=lambda chunk: None,
        on_restart=lambda: None,
    )

    assert result.files[0].remote_path == (
        "/ORIGEN_DOCUMENTOS/CARPETA NIÑO/documento.bin"
    )
    assert listing_client.encoding == "cp1252"
    assert listing_transport.command_encoding == "cp1252"
    assert download_client.encoding == "cp1252"
    assert sum("Windows-1252" in warning for warning in result.warnings) == 1
    assert any(
        b"CARPETA NI\xd1O" in command
        for command in download_client.commands
    )
    assert download_client.retr_calls == 1
    assert b"OPTS UTF8 ON" not in download_client.commands
    assert target.getvalue() == payload
    assert transfer.bytes_received == len(payload)
    worker_transport.close()


def test_ftp_list_fallback_marks_timestamp_unreliable() -> None:
    transport = FtpTransport(
        connection(Protocol.FTP, timezone="America/Bogota"),
        secret="x",
        client=FakeFtp(mlsd_supported=False),
        now=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    with transport:
        result = transport.list_files(("/root",), recursive=False, max_depth=0)
    assert len(result.warnings) == 1
    assert result.files[0].name == "legacy.csv"
    assert not result.files[0].timestamp_reliable
    assert result.files[0].timestamp_source == "LIST"
    assert transport.last_listing_warnings == result.warnings


def test_ftp_list_fallback_aggregates_warning_across_roots() -> None:
    transport = FtpTransport(
        connection(Protocol.FTP, timezone="America/Bogota"),
        secret="x",
        client=FakeFtp(mlsd_supported=False),
        now=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    with transport:
        result = transport.list_files(
            ("/first", "/second"),
            recursive=False,
            max_depth=0,
        )
    assert len(result.files) == 2
    assert len(result.warnings) == 1
    assert transport.last_listing_warnings == result.warnings


def test_ftp_rejects_unparseable_list_line_instead_of_reporting_empty() -> None:
    class ProprietaryListFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__(mlsd_supported=False)

        def retrlines(self, command, callback):
            callback("entrada-propietaria dato-sensible")

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=ProprietaryListFtp(),
    )

    with pytest.raises(RecolectaError) as captured:
        transport.list_files(("/root",), recursive=False, max_depth=0)

    assert captured.value.error_type == ErrorType.PROTOCOL
    assert "LIST" in str(captured.value)
    assert "dato-sensible" not in str(captured.value)


def test_ftp_accepts_standard_empty_list_header() -> None:
    class EmptyListFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__(mlsd_supported=False)

        def retrlines(self, command, callback):
            callback("total 0")

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=EmptyListFtp(),
    )

    result = transport.list_files(("/root",), recursive=False, max_depth=0)

    assert result.files == ()


def test_ftp_rejects_nonempty_list_header_without_any_entries() -> None:
    class TruncatedListFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__(mlsd_supported=False)

        def retrlines(self, command, callback):
            callback("total 8")

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=TruncatedListFtp(),
    )

    with pytest.raises(RecolectaError) as captured:
        transport.list_files(("/root",), recursive=False, max_depth=0)

    assert captured.value.error_type == ErrorType.PROTOCOL


@pytest.mark.parametrize("failed_command", ("SIZE", "MDTM"))
def test_ftp_stat_propagates_transient_metadata_failure(
    failed_command: str,
) -> None:
    class TemporarilyUnavailableFtp(FakeFtp):
        def size(self, path):
            if failed_command == "SIZE":
                raise ftplib.error_temp("450 File temporarily unavailable")
            return 12

        def sendcmd(self, command):
            if failed_command == "MDTM" and command.startswith("MDTM "):
                raise ftplib.error_temp("450 File temporarily unavailable")
            return super().sendcmd(command)

    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=TemporarilyUnavailableFtp(),
    )

    with pytest.raises(ftplib.error_temp, match="450"):
        transport.stat("/root/report.csv")


@pytest.mark.parametrize(
    "directory_lines",
    (
        (
            "drwxr-xr-x 1 owner group 0 Jul 26 03:04 .",
            "drwxr-xr-x 1 owner group 0 Jul 26 03:04 ..",
            "drwxr-xr-x 1 owner group 0 Jul 26 03:04 child",
        ),
        (
            "08-13-26  09:17AM       <DIR>          .",
            "08-13-26  09:17AM       <DIR>          ..",
            "08-13-26  09:17AM       <DIR>          child",
        ),
    ),
    ids=("unix-list", "iis-list"),
)
def test_ftp_list_fallback_never_traverses_dot_directories(
    directory_lines: tuple[str, ...],
) -> None:
    class DotDirectoryFtp(FakeFtp):
        def __init__(self) -> None:
            super().__init__(mlsd_supported=False)
            self.list_commands: list[str] = []

        def retrlines(self, command, callback):
            self.list_commands.append(command)
            if command == "LIST /root":
                for line in directory_lines:
                    callback(line)
                return
            if command == "LIST /root/child":
                callback(
                    "-rw-r--r-- 1 owner group 42 Jul 26 03:05 report.csv"
                )
                return
            raise AssertionError(f"LIST no esperado: {command}")

    client = DotDirectoryFtp()
    transport = FtpTransport(
        connection(Protocol.FTP, timezone="America/Bogota"),
        secret="x",
        client=client,
        now=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    result = transport.list_files(("/root",), recursive=True, max_depth=1)

    assert [item.remote_path for item in result.files] == [
        "/root/child/report.csv"
    ]
    assert client.list_commands == ["LIST /root", "LIST /root/child"]


def test_ftp_resumes_with_rest_and_restarts_when_unsupported() -> None:
    class DownloadFtp:
        def __init__(self, supports_rest: bool):
            self.supports_rest = supports_rest

        def retrbinary(
            self, command, callback, blocksize=8192, rest=None
        ):
            if rest and not self.supports_rest:
                from ftplib import error_perm

                raise error_perm("500 REST not understood")
            content = b"abcdefgh"
            start = rest or 0
            for index in range(start, len(content), blocksize):
                callback(content[index : index + blocksize])
            return "226 Transfer complete"

    for supports_rest, expected_resume in ((True, 3), (False, 0)):
        target = BytesIO(b"abc")
        target.seek(3)
        restarts = []
        transport = FtpTransport(
            connection(Protocol.FTP),
            secret="x",
            client=DownloadFtp(supports_rest),
        )
        with transport:
            result = transport.download_to(
                "/root/a.bin",
                target,
                offset=3,
                block_size=2,
                on_chunk=lambda chunk: None,
                on_restart=lambda: restarts.append(True),
            )
        assert target.getvalue() == b"abcdefgh"
        assert result.resumed_from == expected_resume
        assert result.resume_supported is supports_rest
        assert bool(restarts) is (not supports_rest)


def test_ftp_closes_owned_client_when_authentication_fails(monkeypatch) -> None:
    class RejectedFtp:
        def __init__(self):
            self.closed = False

        def connect(self, host, port, timeout):
            return None

        def login(self, username, secret):
            raise ftplib.error_perm("530 Login incorrect")

        def quit(self):
            raise RuntimeError("La sesión no llegó a autenticarse.")

        def close(self):
            self.closed = True

    client = RejectedFtp()
    monkeypatch.setattr(ftplib, "FTP", lambda: client)
    transport = FtpTransport(connection(Protocol.FTP), secret="incorrecta")

    with pytest.raises(ftplib.error_perm):
        transport.connect()

    assert client.closed is True
    assert transport._ftp is None


class FakeSftp:
    def __init__(self) -> None:
        self.entries = {
            "/root": [
                _sftp_attr("a.csv", stat.S_IFREG | 0o644, 10, 1_700_000_000),
                _sftp_attr("sub", stat.S_IFDIR | 0o755, 0, 1_700_000_001),
                _sftp_attr("link", stat.S_IFLNK | 0o777, 5, 1_700_000_002),
            ],
            "/root/sub": [
                _sftp_attr("b.csv", stat.S_IFREG | 0o644, 20, 1_700_000_003)
            ],
        }

    def listdir_attr(self, path):
        return self.entries[path]

    def lstat(self, path):
        parent, name = path.rsplit("/", 1)
        return next(item for item in self.entries[parent or "/"] if item.filename == name)


def _sftp_attr(name: str, mode: int, size: int, modified: int):
    value = paramiko.SFTPAttributes()
    value.filename = name
    value.st_mode = mode
    value.st_size = size
    value.st_mtime = modified
    return value


def test_sftp_lists_recursively_without_following_symlinks(tmp_path: Path) -> None:
    transport = SftpTransport(
        connection(Protocol.SFTP),
        secret="x",
        known_hosts=tmp_path / "known_hosts",
        sftp_client=FakeSftp(),
    )
    with transport:
        result = transport.list_files(("/root",), recursive=True, max_depth=1)
        metadata = transport.stat("/root/a.csv")
    assert {item.name for item in result.files} == {"a.csv", "b.csv", "link"}
    assert next(item for item in result.files if item.name == "link").is_symlink
    assert metadata.size_bytes == 10


def test_sftp_prefers_lazy_listdir_iter(tmp_path: Path) -> None:
    consumed: list[str] = []

    class LazySftp:
        def listdir_iter(self, path):
            for name in ("first.csv", "second.csv"):
                consumed.append(name)
                yield _sftp_attr(
                    name,
                    stat.S_IFREG | 0o644,
                    10,
                    1_700_000_000,
                )

        def listdir_attr(self, path):
            raise AssertionError("No debe materializar listdir_attr.")

    transport = SftpTransport(
        connection(Protocol.SFTP),
        secret="x",
        known_hosts=tmp_path / "known_hosts",
        sftp_client=LazySftp(),
    )
    with transport:
        files = transport.iter_files(
            ("/root",),
            recursive=False,
            max_depth=0,
        )
        assert next(files).name == "first.csv"
        assert consumed == ["first.csv"]
        files.close()


def test_sftp_connect_enables_tofu_and_disables_ambient_credentials(
    tmp_path: Path,
) -> None:
    class FakeSsh:
        def __init__(self):
            self.policy = None
            self.arguments = None

        def load_host_keys(self, path):
            self.host_keys_path = path

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **arguments):
            self.arguments = arguments

        def open_sftp(self):
            return FakeSftp()

    ssh = FakeSsh()
    known_hosts = tmp_path / "data" / "known_hosts"
    transport = SftpTransport(
        connection(Protocol.SFTP),
        secret="password",
        known_hosts=known_hosts,
        ssh_client=ssh,
    )
    transport.connect()
    assert known_hosts.is_file()
    assert isinstance(ssh.policy, paramiko.AutoAddPolicy)
    assert ssh.arguments["look_for_keys"] is False
    assert ssh.arguments["allow_agent"] is False
    assert ssh.arguments["password"] == "password"
    assert ssh.arguments["channel_timeout"] == 30.0


def test_sftp_applies_channel_timeout_to_injected_client(tmp_path: Path) -> None:
    class Channel:
        timeout = None

        def settimeout(self, value):
            self.timeout = value

    class TimedSftp(FakeSftp):
        def __init__(self) -> None:
            super().__init__()
            self.channel = Channel()

        def get_channel(self):
            return self.channel

    client = TimedSftp()
    transport = SftpTransport(
        connection(Protocol.SFTP, timeout_s=7.5),
        secret="x",
        known_hosts=tmp_path / "known_hosts",
        sftp_client=client,
    )

    transport.connect()

    assert client.channel.timeout == 7.5


def test_sftp_closes_owned_client_when_authentication_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class RejectedSsh:
        def __init__(self):
            self.closed = False

        def load_host_keys(self, path):
            return None

        def set_missing_host_key_policy(self, policy):
            return None

        def connect(self, **arguments):
            raise paramiko.AuthenticationException("Authentication failed")

        def close(self):
            self.closed = True

    client = RejectedSsh()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    transport = SftpTransport(
        connection(Protocol.SFTP),
        secret="incorrecta",
        known_hosts=tmp_path / "known_hosts",
    )

    with pytest.raises(paramiko.AuthenticationException):
        transport.connect()

    assert client.closed is True
    assert transport._ssh is None


def test_sftp_download_seeks_to_partial_offset(tmp_path: Path) -> None:
    class DownloadSftp(FakeSftp):
        def open(self, path, mode):
            return BytesIO(b"abcdefgh")

    target = BytesIO(b"abc")
    target.seek(3)
    transport = SftpTransport(
        connection(Protocol.SFTP),
        secret="x",
        known_hosts=tmp_path / "known_hosts",
        sftp_client=DownloadSftp(),
    )
    with transport:
        result = transport.download_to(
            "/root/a.bin",
            target,
            offset=3,
            block_size=2,
            on_chunk=lambda chunk: None,
            on_restart=lambda: None,
        )
    assert target.getvalue() == b"abcdefgh"
    assert result.resumed_from == 3


def _dav_multistatus(entries: list[tuple[str, bool, int | None]]) -> bytes:
    responses = []
    for path, is_directory, size in entries:
        resource_type = "<d:collection/>" if is_directory else ""
        size_xml = "" if size is None else f"<d:getcontentlength>{size}</d:getcontentlength>"
        responses.append(
            f"""
            <d:response>
              <d:href>{path}</d:href>
              <d:propstat><d:prop>
                <d:resourcetype>{resource_type}</d:resourcetype>
                {size_xml}
                <d:getlastmodified>Sun, 26 Jul 2026 03:04:05 GMT</d:getlastmodified>
              </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
            </d:response>
            """
        )
    return (
        '<d:multistatus xmlns:d="DAV:">'
        + "".join(responses)
        + "</d:multistatus>"
    ).encode()


def test_webdav_propfind_lists_recursively_and_never_uses_get() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("Depth")))
        assert request.method == "PROPFIND"
        if request.url.path == "/root/sub":
            entries = [
                ("/root/sub/", True, None),
                ("/root/sub/b.csv", False, 20),
            ]
        elif request.headers.get("Depth") == "0":
            entries = [("/root/a.csv", False, 10)]
        else:
            entries = [
                ("/root/", True, None),
                ("/root/a.csv", False, 10),
                ("/root/sub/", True, None),
            ]
        return httpx.Response(207, content=_dav_multistatus(entries))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = WebDavTransport(
        connection(Protocol.WEBDAV),
        secret="x",
        client=client,
    )
    with transport:
        result = transport.list_files(("/root",), recursive=True, max_depth=1)
        metadata = transport.stat("/root/a.csv")
    assert {item.name for item in result.files} == {"a.csv", "b.csv"}
    assert metadata.size_bytes == 10
    assert metadata.mtime_utc == datetime(
        2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc
    )
    assert all(method == "PROPFIND" for method, _, _ in calls)
    client.close()


@pytest.mark.parametrize(
    ("ssl_mode", "verify"),
    (("required", True), ("insecure", False)),
)
def test_webdavs_certificate_verification_is_explicit(
    monkeypatch,
    ssl_mode: str,
    verify: bool,
) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def close(self):
            return None

    monkeypatch.setattr(httpx, "Client", FakeClient)
    transport = WebDavTransport(
        connection(Protocol.WEBDAVS, ssl_mode=ssl_mode),
        secret="x",
    )

    transport.connect()
    transport.close()

    assert captured["verify"] is verify
    assert captured["follow_redirects"] is True
    assert captured["event_hooks"]["request"]


def test_webdavs_rejects_http_endpoint_and_downgrade() -> None:
    with pytest.raises(ValueError, match="requiere https"):
        WebDavTransport(
            connection(
                Protocol.WEBDAVS,
                host="http://example.test/dav",
            ),
            secret="x",
        )

    transport = WebDavTransport(
        connection(
            Protocol.WEBDAVS,
            host="https://example.test/dav",
        ),
        secret="x",
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )
    with pytest.raises(RecolectaError) as captured:
        transport._validate_request_scheme(
            httpx.Request("GET", "http://example.test/dav/root")
        )
    assert captured.value.error_type == ErrorType.TLS
    transport._client.close()


def test_webdav_preserves_endpoint_base_path_and_strips_it_from_href() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dav/root"
        return httpx.Response(
            207,
            content=_dav_multistatus(
                [
                    ("/dav/root/", True, None),
                    ("/dav/root/a.csv", False, 10),
                ]
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = WebDavTransport(
        connection(
            Protocol.WEBDAV,
            host="http://example.test/dav",
        ),
        secret="x",
        client=client,
    )

    with transport:
        result = transport.list_files(("/root",), recursive=False, max_depth=0)

    assert result.files[0].remote_path == "/root/a.csv"
    client.close()


def test_webdav_iter_files_requests_subdirectories_incrementally() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/root/sub":
            entries = [
                ("/root/sub/", True, None),
                ("/root/sub/b.csv", False, 20),
            ]
        else:
            entries = [
                ("/root/", True, None),
                ("/root/a.csv", False, 10),
                ("/root/sub/", True, None),
            ]
        return httpx.Response(207, content=_dav_multistatus(entries))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = WebDavTransport(
        connection(Protocol.WEBDAV),
        secret="x",
        client=client,
    )
    with transport:
        files = transport.iter_files(
            ("/root",),
            recursive=True,
            max_depth=1,
        )
        assert next(files).name == "a.csv"
        assert calls == ["/root"]
        assert [item.name for item in files] == ["b.csv"]
        assert calls == ["/root", "/root/sub"]
    client.close()


def test_webdav_range_resume_and_200_restart() -> None:
    content = b"abcdefgh"
    for supports_range, expected_resume in ((True, 3), (False, 0)):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            if supports_range:
                assert request.headers["Range"] == "bytes=3-"
                return httpx.Response(
                    206,
                    stream=httpx.ByteStream(content[3:]),
                    headers={"Content-Range": "bytes 3-7/8"},
                )
            return httpx.Response(200, stream=httpx.ByteStream(content))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        target = BytesIO(b"abc")
        target.seek(3)
        restarts = []
        transport = WebDavTransport(
            connection(Protocol.WEBDAV),
            secret="x",
            client=client,
        )
        with transport:
            result = transport.download_to(
                "/root/a.bin",
                target,
                offset=3,
                block_size=2,
                on_chunk=lambda chunk: None,
                on_restart=lambda: restarts.append(True),
            )
        assert target.getvalue() == content
        assert result.resumed_from == expected_resume
        assert result.resume_supported is supports_range
        assert bool(restarts) is (not supports_range)
        client.close()


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"Content-Range": "bytes 2-7/8"},
        {"Content-Range": "not-a-range"},
    ),
)
def test_webdav_rejects_unconfirmed_partial_ranges(
    headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Range"] == "bytes=3-"
        return httpx.Response(206, content=b"defgh", headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    target = BytesIO(b"abc")
    target.seek(3)
    transport = WebDavTransport(
        connection(Protocol.WEBDAV),
        secret="x",
        client=client,
    )

    with transport, pytest.raises(RuntimeError, match="rango exacto"):
        transport.download_to(
            "/root/a.bin",
            target,
            offset=3,
            block_size=2,
            on_chunk=lambda chunk: None,
            on_restart=lambda: None,
        )

    assert target.getvalue() == b"abc"
    client.close()


def test_webdav_download_preserves_raw_content_encoding_bytes() -> None:
    encoded = gzip.compress(b"\xff\xfecontenido\r\n", mtime=0)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            stream=httpx.ByteStream(encoded),
            headers={"Content-Encoding": "gzip"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    target = BytesIO()
    transport = WebDavTransport(
        connection(Protocol.WEBDAV),
        secret="x",
        client=client,
    )
    with transport:
        result = transport.download_to(
            "/root/a.bin",
            target,
            offset=0,
            block_size=3,
            on_chunk=lambda chunk: None,
            on_restart=lambda: None,
        )

    assert target.getvalue() == encoded
    assert result.bytes_received == len(encoded)
    client.close()


def test_smb_lists_local_tree_for_development(tmp_path: Path) -> None:
    root = tmp_path / "share"
    nested = root / "nested"
    nested.mkdir(parents=True)
    first = root / "a.csv"
    second = nested / "b.csv"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    modified = datetime(2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    os.utime(first, (modified, modified))
    os.utime(second, (modified, modified))

    transport = SmbTransport(
        connection(Protocol.SMB, remote_paths=(str(root),)),
        secret=None,
    )
    with transport:
        result = transport.list_files((str(root),), recursive=True, max_depth=1)
        metadata = transport.stat(str(second))
    assert {item.name for item in result.files} == {"a.csv", "b.csv"}
    assert metadata.size_bytes == 2
    assert metadata.mtime_utc == datetime(
        2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc
    )


def test_smb_iter_files_consumes_scandir_lazily(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "share"
    root.mkdir()
    (root / "first.bin").write_bytes(b"first")
    (root / "second.bin").write_bytes(b"second")
    real_scandir = os.scandir
    consumed: list[str] = []

    class TrackingScandir:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self._entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._entries)
            consumed.append(entry.name)
            return entry

    monkeypatch.setattr(
        "app.transports.smb.os.scandir",
        lambda path: TrackingScandir(path),
    )
    transport = SmbTransport(
        connection(Protocol.SMB, remote_paths=(str(root),)),
        secret=None,
    )
    with transport:
        files = transport.iter_files(
            (str(root),),
            recursive=False,
            max_depth=0,
        )
        first = next(files)
        assert first.name in {"first.bin", "second.bin"}
        assert len(consumed) == 1
        assert {first.name, *(item.name for item in files)} == {
            "first.bin",
            "second.bin",
        }


def test_smb_download_resumes_from_offset(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abcdefgh")
    target = BytesIO(b"abc")
    target.seek(3)
    transport = SmbTransport(
        connection(Protocol.SMB, remote_paths=(str(source),)),
        secret=None,
    )
    with transport:
        result = transport.download_to(
            str(source),
            target,
            offset=3,
            block_size=2,
            on_chunk=lambda chunk: None,
            on_restart=lambda: None,
        )
    assert target.getvalue() == b"abcdefgh"
    assert result.resumed_from == 3


def test_smb_applies_connection_and_operation_timeouts(monkeypatch) -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.receive_calls: list[tuple[object, bool, float | None, bool]] = []
            self.connect_calls: list[float] = []
            self.transport = SimpleNamespace(connected=False)

        def connect(self, *, timeout):
            self.connect_calls.append(timeout)
            self.transport.connected = True

        def receive(
            self,
            request,
            wait=True,
            timeout=None,
            resolve_symlinks=True,
        ):
            self.receive_calls.append(
                (request, wait, timeout, resolve_symlinks)
            )
            return "response"

    class FakeClient:
        def __init__(self) -> None:
            self.connection = FakeConnection()
            self.registered = None
            self.reset = None

        def register_session(self, host, **kwargs):
            self.registered = (host, kwargs)
            return SimpleNamespace(connection=self.connection)

        def reset_connection_cache(self, **kwargs):
            self.reset = kwargs

    transport = SmbTransport(
        connection(
            Protocol.SMB,
            host="server",
            username=r"DOMAIN\operator",
            remote_paths=(r"\\server\share",),
            timeout_s=7.5,
        ),
        secret="password",
    )
    client = FakeClient()
    transport._smb_client = client
    monkeypatch.setattr(
        "app.transports.smb._new_protocol_connection",
        lambda smbclient, host, port: client.connection,
    )

    transport.connect()
    assert client.connection.connect_calls == [7.5]
    assert client.registered is not None
    host, registered = client.registered
    assert host == "server"
    assert registered == {
        "username": r"DOMAIN\operator",
        "password": "password",
        "port": 445,
        "connection_timeout": 7.5,
        "connection_cache": transport._connection_cache,
    }
    assert client.connection.receive("default") == "response"
    assert client.connection.receive("short", timeout=1.25) == "response"
    assert client.connection.receive("long", timeout=60) == "response"
    assert client.connection.receive_calls == [
        ("default", True, 7.5, True),
        ("short", True, 1.25, True),
        ("long", True, 7.5, True),
    ]

    transport.close()
    assert client.reset == {
        "fail_on_error": False,
        "connection_cache": {},
    }


def test_smb_remote_operations_use_private_bounded_session(monkeypatch) -> None:
    modified = datetime(2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc)

    class Entry:
        path = r"\\server\share\payload.bin"

        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def is_dir(*, follow_symlinks):
            return False

        @staticmethod
        def is_file(*, follow_symlinks):
            return True

        @staticmethod
        def stat(*, follow_symlinks):
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=5,
                st_mtime=modified.timestamp(),
            )

    class Entries:
        def __enter__(self):
            return iter((Entry(),))

        def __exit__(self, exc_type, exc, traceback):
            return False

    class FakeConnection:
        def __init__(self) -> None:
            self.transport = SimpleNamespace(connected=False)

        def connect(self, *, timeout):
            self.transport.connected = True

        @staticmethod
        def receive(
            request,
            wait=True,
            timeout=None,
            resolve_symlinks=True,
        ):
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []
            self.connection = FakeConnection()

        def register_session(self, host, **kwargs):
            return SimpleNamespace(connection=self.connection)

        @staticmethod
        def reset_connection_cache(**kwargs):
            return None

        def stat(self, path, **kwargs):
            self.calls.append(("stat", path, kwargs))
            return SimpleNamespace(
                st_mode=(
                    stat.S_IFDIR if path == r"\\server\share" else stat.S_IFREG
                ),
                st_size=0 if path == r"\\server\share" else 5,
                st_mtime=modified.timestamp(),
            )

        def scandir(self, path, **kwargs):
            self.calls.append(("scandir", path, kwargs))
            return Entries()

        def open_file(self, path, **kwargs):
            self.calls.append(("open_file", path, kwargs))
            return BytesIO(b"\x00\xffabc")

    transport = SmbTransport(
        connection(
            Protocol.SMB,
            host="server",
            remote_paths=(r"\\server\share",),
            timeout_s=9.0,
        ),
        secret="password",
    )
    client = FakeClient()
    transport._smb_client = client
    monkeypatch.setattr(
        "app.transports.smb._new_protocol_connection",
        lambda smbclient, host, port: client.connection,
    )
    target = BytesIO()

    with transport:
        files = list(
            transport.iter_files(
                (r"\\server\share",),
                recursive=False,
                max_depth=0,
            )
        )
        result = transport.download_to(
            files[0].remote_path,
            target,
            offset=0,
            block_size=2,
            on_chunk=lambda chunk: None,
            on_restart=lambda: None,
        )

    assert files[0].size_bytes == 5
    assert target.getvalue() == b"\x00\xffabc"
    assert result.bytes_received == 5
    for _operation, _path, kwargs in client.calls:
        assert kwargs["connection_timeout"] == 9.0
        assert kwargs["connection_cache"] is transport._connection_cache


def test_smb_rejects_unc_path_for_another_host() -> None:
    transport = SmbTransport(
        connection(
            Protocol.SMB,
            host="server",
            remote_paths=(r"\\other\share",),
        ),
        secret="password",
    )

    with pytest.raises(ValueError, match="host configurado"):
        transport.connect()


def test_transport_factory_selects_all_protocols(tmp_path: Path) -> None:
    expected = {
        Protocol.FTP: "FtpTransport",
        Protocol.FTPS: "FtpTransport",
        Protocol.SFTP: "SftpTransport",
        Protocol.WEBDAV: "WebDavTransport",
        Protocol.WEBDAVS: "WebDavTransport",
        Protocol.SMB: "SmbTransport",
    }
    for protocol, class_name in expected.items():
        transport = create_transport(
            connection(protocol),
            secret=None,
            known_hosts=tmp_path / "known_hosts",
        )
        assert type(transport).__name__ == class_name
