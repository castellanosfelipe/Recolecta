import ipaddress
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.log import config_logging
from pyftpdlib.servers import FTPServer

from app.connection_validation import validate_connection_paths
from app.errors import ErrorType, RecolectaError
from app.models import Connection, Protocol
from app.transports.ftp import FtpTransport


@dataclass
class RunningFtp:
    root: Path
    port: int
    server: FTPServer
    thread: threading.Thread


@pytest.fixture
def ftp_server_factory(tmp_path: Path):
    running: list[RunningFtp] = []
    config_logging(level=logging.ERROR)

    def start(*, tls: bool) -> RunningFtp:
        root = tmp_path / ("ftps-root" if tls else "ftp-root")
        (root / "entrada" / "nested").mkdir(parents=True)
        (root / "entrada" / "today.csv").write_bytes(b"today")
        (root / "entrada" / "nested" / "deep.csv").write_bytes(b"deep")
        modified = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).replace(microsecond=0).timestamp()
        os.utime(root / "entrada" / "today.csv", (modified, modified))
        os.utime(root / "entrada" / "nested" / "deep.csv", (modified, modified))

        authorizer = DummyAuthorizer()
        authorizer.add_user(
            "operator",
            "password",
            str(root),
            perm="elradfmwMT",
        )
        if tls:
            pytest.importorskip("OpenSSL")
            from pyftpdlib.handlers import TLS_FTPHandler

            certificate = _create_certificate(tmp_path / "server.pem")

            class Handler(TLS_FTPHandler):
                pass

            Handler.certfile = str(certificate)
            Handler.tls_control_required = True
            Handler.tls_data_required = True
        else:

            class Handler(FTPHandler):
                pass

        Handler.authorizer = authorizer
        server = FTPServer(("127.0.0.1", 0), Handler)
        port = server.socket.getsockname()[1]
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"timeout": 0.05, "blocking": True, "handle_exit": False},
            daemon=True,
        )
        thread.start()
        value = RunningFtp(root, port, server, thread)
        running.append(value)
        return value

    yield start

    for value in running:
        value.server.close_all()
        value.thread.join(timeout=2)


@pytest.mark.parametrize("tls", [False, True], ids=["ftp", "ftps"])
def test_real_ftp_and_ftps_listing_is_recursive_and_uses_utc_mdtm(
    ftp_server_factory,
    tls: bool,
) -> None:
    server = ftp_server_factory(tls=tls)
    protocol = Protocol.FTPS if tls else Protocol.FTP
    connection = Connection(
        name=f"Local {protocol}",
        protocol=protocol,
        host="127.0.0.1",
        port=server.port,
        username="operator",
        remote_paths=("/entrada",),
        recursive=True,
        max_depth=1,
        dest_root="downloads",
        ssl_mode="preferred",
    ).normalized()
    transport = FtpTransport(connection, secret="password")
    target = BytesIO(b"to")
    target.seek(2)
    restarts = []
    with transport:
        result = transport.list_files(
            connection.remote_paths,
            recursive=True,
            max_depth=1,
        )
        metadata = transport.stat("/entrada/today.csv")
        transfer = transport.download_to(
            "/entrada/today.csv",
            target,
            offset=2,
            block_size=1024,
            on_chunk=lambda chunk: None,
            on_restart=lambda: restarts.append(True),
        )
    assert {item.name for item in result.files} == {"today.csv", "deep.csv"}
    assert result.warnings == ()
    assert all(item.timestamp_source == "MDTM" for item in result.files)
    assert metadata.size_bytes == 5
    assert metadata.mtime_utc is not None
    assert metadata.mtime_utc.tzinfo == timezone.utc
    assert target.getvalue() == b"today"
    assert transfer.resumed_from == 2
    assert transfer.resume_supported
    assert restarts == []


def test_pre_save_validation_accepts_valid_ftp_paths_and_explains_bad_password(
    ftp_server_factory,
    tmp_path: Path,
) -> None:
    server = ftp_server_factory(tls=False)
    destination = tmp_path / "validated-downloads"
    connection = Connection(
        name="Validación FTP",
        protocol=Protocol.FTP,
        host="127.0.0.1",
        port=server.port,
        username="operator",
        remote_paths=("/entrada",),
        dest_root=str(destination),
    )

    result = validate_connection_paths(
        connection,
        secret="password",
        portable_root=tmp_path,
        known_hosts=tmp_path / "known_hosts",
    )

    assert result.remote_paths == ("/entrada",)
    assert result.remote_files_found == 1
    assert not destination.exists()

    with pytest.raises(RecolectaError) as captured:
        validate_connection_paths(
            connection,
            secret="incorrecta",
            portable_root=tmp_path,
            known_hosts=tmp_path / "known_hosts",
        )

    assert captured.value.error_type == ErrorType.AUTH
    assert "credencial fue rechazada" in str(captured.value).lower()
    assert "incorrecta" not in str(captured.value)


def _create_certificate(path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        + certificate.public_bytes(serialization.Encoding.PEM)
    )
    return path
