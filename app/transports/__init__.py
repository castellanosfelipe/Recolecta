"""File-transfer transport implementations."""

from __future__ import annotations

from pathlib import Path

from app.models import Connection, Protocol
from app.transports.base import Transport


def create_transport(
    connection: Connection,
    *,
    secret: str | None,
    known_hosts: Path | None = None,
    ftp_command_encoding: str | None = None,
) -> Transport:
    """Create the protocol adapter selected by a validated connection."""
    protocol = connection.protocol
    if protocol in {Protocol.FTP, Protocol.FTPS}:
        from app.transports.ftp import FtpTransport

        return FtpTransport(
            connection,
            secret=secret,
            command_encoding=ftp_command_encoding,
        )
    if protocol == Protocol.SFTP:
        from app.transports.sftp import SftpTransport

        if known_hosts is None:
            raise ValueError("SFTP requiere la ruta local de known_hosts.")
        return SftpTransport(connection, secret=secret, known_hosts=known_hosts)
    if protocol in {Protocol.WEBDAV, Protocol.WEBDAVS}:
        from app.transports.webdav import WebDavTransport

        return WebDavTransport(connection, secret=secret)
    if protocol == Protocol.SMB:
        from app.transports.smb import SmbTransport

        return SmbTransport(connection, secret=secret)
    raise ValueError(f"Protocolo no soportado: {protocol}.")
