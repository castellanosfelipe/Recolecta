import ftplib
import gzip
import os
import stat
import sys
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO, StringIO
from pathlib import Path

import httpx
import paramiko
import pytest

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


class _FtpListingSocket:
    def __init__(self) -> None:
        self.closed = False

    def makefile(self, mode, *, encoding, newline):
        assert mode == "r"
        assert encoding == "utf-8"
        assert newline == ""
        return StringIO(
            "type=file;size=1;modify=20260101010101; first.csv\r\n"
            "type=file;size=1;modify=20260101010101; second.csv\r\n"
        )

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


def test_smb_builds_netresource_for_explicit_credentials(
    monkeypatch,
) -> None:
    if sys.platform != "win32":
        return
    import win32netcon
    import win32wnet

    captured = {}

    def record(resource, password, username, flags):
        captured.update(
            remote=resource.lpRemoteName,
            resource_type=resource.dwType,
            password=password,
            username=username,
            flags=flags,
        )

    monkeypatch.setattr(win32wnet, "WNetAddConnection2", record)
    transport = SmbTransport(
        connection(
            Protocol.SMB,
            host="server",
            username=r"DOMAIN\operator",
        ),
        secret="password",
    )
    transport._ensure_credentials(Path(r"\\server\share\folder"))
    assert captured == {
        "remote": r"\\server\share",
        "resource_type": win32netcon.RESOURCETYPE_DISK,
        "password": "password",
        "username": r"DOMAIN\operator",
        "flags": 0,
    }


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
