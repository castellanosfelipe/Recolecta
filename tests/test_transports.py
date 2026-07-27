import os
import stat
import sys
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import httpx
import paramiko

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


def test_ftp_prefers_mdtm_over_mlsd_modify() -> None:
    transport = FtpTransport(
        connection(Protocol.FTP),
        secret="x",
        client=FakeFtp(),
    )
    with transport:
        result = transport.list_files(("/root",), recursive=False, max_depth=0)
    assert result.warnings == ()
    assert result.files[0].timestamp_source == "MDTM"
    assert result.files[0].mtime_utc == datetime(
        2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc
    )


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


def test_webdav_range_resume_and_200_restart() -> None:
    content = b"abcdefgh"
    for supports_range, expected_resume in ((True, 3), (False, 0)):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            if supports_range:
                assert request.headers["Range"] == "bytes=3-"
                return httpx.Response(
                    206,
                    content=content[3:],
                    headers={"Content-Range": "bytes 3-7/8"},
                )
            return httpx.Response(200, content=content)

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
