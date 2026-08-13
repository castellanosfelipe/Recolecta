"""Socket-level integration smoke tests for SFTP and WebDAV transports."""

from __future__ import annotations

import base64
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

import paramiko
import pytest

from app.models import Connection, Protocol
from app.transports.sftp import SftpTransport
from app.transports.webdav import WebDavTransport


BINARY_CONTENT = b"\x00\xffcabecera\r\ncontenido-binario\x00final"
MODIFIED = datetime(2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RunningServer:
    port: int


class _SftpAuthServer(paramiko.ServerInterface):
    def check_auth_password(self, username: str, password: str) -> int:
        if username == "operator" and password == "password":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _LocalSftpInterface(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root: Path, **kwargs) -> None:
        super().__init__(server, *args, **kwargs)
        self.root = root

    def _local(self, remote_path: str) -> Path:
        parts = [
            part
            for part in remote_path.replace("\\", "/").split("/")
            if part not in {"", "."}
        ]
        if ".." in parts:
            raise PermissionError("Traversal SFTP rechazado por el servidor de prueba.")
        return self.root.joinpath(*parts)

    def list_folder(self, path: str):
        try:
            result = []
            for child in self._local(path).iterdir():
                attributes = paramiko.SFTPAttributes.from_stat(child.lstat())
                attributes.filename = child.name
                result.append(attributes)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path: str):
        try:
            return paramiko.SFTPAttributes.from_stat(self._local(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path: str):
        try:
            return paramiko.SFTPAttributes.from_stat(self._local(path).lstat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path: str, flags: int, attr):
        try:
            descriptor = os.open(self._local(path), flags)
            stream = os.fdopen(descriptor, "rb")
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = stream
        return handle


@pytest.fixture
def real_sftp_server(tmp_path: Path):
    root = tmp_path / "sftp-root"
    nested = root / "entrada" / "nested"
    nested.mkdir(parents=True)
    payload = root / "entrada" / "payload.bin"
    payload.write_bytes(BINARY_CONTENT)
    (nested / "deep.bin").write_bytes(b"\x00deep\xff")
    modified = MODIFIED.timestamp()
    os.utime(payload, (modified, modified))

    host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client)
            try:
                transport.add_server_key(host_key)
                transport.set_subsystem_handler(
                    "sftp",
                    paramiko.SFTPServer,
                    _LocalSftpInterface,
                    root=root,
                )
                transport.start_server(server=_SftpAuthServer())
                while transport.is_active() and not stop.wait(0.02):
                    pass
            finally:
                transport.close()
                client.close()

    thread = threading.Thread(target=serve, daemon=True, name="test-sftp")
    thread.start()
    yield RunningServer(port)
    stop.set()
    listener.close()
    thread.join(timeout=3)
    assert not thread.is_alive()


class _DavServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # A timed-out client may close while a test response is being written.
        return None


class _DavHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    expected_auth = "Basic " + base64.b64encode(
        b"operator:password"
    ).decode("ascii")
    files = {
        "/dav/entrada/payload.bin": BINARY_CONTENT,
        "/dav/entrada/nested/deep.bin": b"\x00deep\xff",
    }

    def do_PROPFIND(self) -> None:  # noqa: N802 - HTTP method name
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        path = unquote(urlsplit(self.path).path).rstrip("/") or "/"
        entries = self._entries(path)
        if entries is None:
            self._empty(404)
            return
        responses = "".join(
            self._dav_response(entry_path, is_directory, size)
            for entry_path, is_directory, size in entries
        )
        document = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:">'
            f"{responses}</d:multistatus>"
        ).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.end_headers()
        self.wfile.write(document)

    def do_GET(self) -> None:  # noqa: N802 - HTTP method name
        if not self._authorized():
            return
        path = unquote(urlsplit(self.path).path)
        content = self.files.get(path)
        if content is None:
            self._empty(404)
            return
        range_header = self.headers.get("Range")
        status = 200
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            body = content[start:]
            status = 206
        else:
            start = 0
            body = content
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        if status == 206:
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(content) - 1}/{len(content)}",
            )
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == self.expected_auth:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Recolecta test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    @classmethod
    def _entries(
        cls,
        path: str,
    ) -> list[tuple[str, bool, int | None]] | None:
        if path == "/dav/entrada":
            return [
                ("/dav/entrada/", True, None),
                (
                    "/dav/entrada/payload.bin",
                    False,
                    len(cls.files["/dav/entrada/payload.bin"]),
                ),
                ("/dav/entrada/nested/", True, None),
            ]
        if path == "/dav/entrada/nested":
            return [
                ("/dav/entrada/nested/", True, None),
                (
                    "/dav/entrada/nested/deep.bin",
                    False,
                    len(cls.files["/dav/entrada/nested/deep.bin"]),
                ),
            ]
        content = cls.files.get(path)
        if content is not None:
            return [(path, False, len(content))]
        return None

    @staticmethod
    def _dav_response(path: str, is_directory: bool, size: int | None) -> str:
        resource_type = "<d:collection/>" if is_directory else ""
        size_xml = "" if size is None else f"<d:getcontentlength>{size}</d:getcontentlength>"
        modified = format_datetime(MODIFIED, usegmt=True)
        return (
            "<d:response>"
            f"<d:href>{escape(path)}</d:href>"
            "<d:propstat><d:prop>"
            f"<d:resourcetype>{resource_type}</d:resourcetype>"
            f"{size_xml}<d:getlastmodified>{modified}</d:getlastmodified>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return None


@pytest.fixture
def real_webdav_server():
    server = _DavServer(("127.0.0.1", 0), _DavHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
        name="test-webdav",
    )
    thread.start()
    yield RunningServer(server.server_port)
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_real_sftp_lists_and_resumes_binary_without_transformation(
    real_sftp_server: RunningServer,
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    connection = Connection(
        name="SFTP local real",
        protocol=Protocol.SFTP,
        host="127.0.0.1",
        port=real_sftp_server.port,
        username="operator",
        remote_paths=("/entrada",),
        recursive=True,
        max_depth=1,
        timeout_s=2,
        dest_root=str(tmp_path / "downloads"),
    ).normalized()
    target = BytesIO(BINARY_CONTENT[:5])
    target.seek(5)

    with SftpTransport(
        connection,
        secret="password",
        known_hosts=known_hosts,
    ) as transport:
        listing = transport.list_files(
            connection.remote_paths,
            recursive=True,
            max_depth=1,
        )
        metadata = transport.stat("/entrada/payload.bin")
        transfer = transport.download_to(
            "/entrada/payload.bin",
            target,
            offset=5,
            block_size=1024,
            on_chunk=lambda chunk: None,
            on_restart=lambda: pytest.fail("SFTP no debía reiniciar el parcial."),
        )

    assert {item.name for item in listing.files} == {"payload.bin", "deep.bin"}
    assert metadata.size_bytes == len(BINARY_CONTENT)
    assert target.getvalue() == BINARY_CONTENT
    assert transfer.resumed_from == 5
    assert transfer.resume_supported is True
    assert known_hosts.read_text(encoding="utf-8").strip()


def test_real_webdav_lists_base_path_and_resumes_raw_binary(
    real_webdav_server: RunningServer,
    tmp_path: Path,
) -> None:
    connection = Connection(
        name="WebDAV local real",
        protocol=Protocol.WEBDAV,
        host=f"http://127.0.0.1:{real_webdav_server.port}/dav",
        username="operator",
        remote_paths=("/entrada",),
        recursive=True,
        max_depth=1,
        timeout_s=2,
        dest_root=str(tmp_path / "downloads"),
    ).normalized()
    target = BytesIO(BINARY_CONTENT[:5])
    target.seek(5)

    with WebDavTransport(connection, secret="password") as transport:
        listing = transport.list_files(
            connection.remote_paths,
            recursive=True,
            max_depth=1,
        )
        metadata = transport.stat("/entrada/payload.bin")
        transfer = transport.download_to(
            "/entrada/payload.bin",
            target,
            offset=5,
            block_size=7,
            on_chunk=lambda chunk: None,
            on_restart=lambda: pytest.fail("WebDAV confirmó soporte Range."),
        )

    assert {item.remote_path for item in listing.files} == {
        "/entrada/payload.bin",
        "/entrada/nested/deep.bin",
    }
    assert metadata.size_bytes == len(BINARY_CONTENT)
    assert target.getvalue() == BINARY_CONTENT
    assert transfer.resumed_from == 5
    assert transfer.resume_supported is True
