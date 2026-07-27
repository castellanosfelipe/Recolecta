"""SFTP metadata listing with TOFU host-key persistence."""

from __future__ import annotations

import posixpath
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

import paramiko

from app.models import AuthType, Connection
from app.transports.base import ListingResult, RemoteFile, TransferResult, Transport


class SftpTransport(Transport):
    """List SFTP attributes without reading file contents."""

    def __init__(
        self,
        connection: Connection,
        *,
        secret: str | None,
        known_hosts: Path,
        ssh_client: paramiko.SSHClient | None = None,
        sftp_client: paramiko.SFTPClient | None = None,
    ) -> None:
        self.connection = connection.normalized()
        self.secret = secret
        self.known_hosts = known_hosts
        self._ssh = ssh_client
        self._sftp = sftp_client
        self._owns_clients = ssh_client is None and sftp_client is None

    def connect(self) -> None:
        if self._sftp is not None:
            return
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.touch(mode=0o600, exist_ok=True)
        client = self._ssh or paramiko.SSHClient()
        client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        arguments = {
            "hostname": self.connection.host,
            "port": self.connection.port or 22,
            "username": self.connection.username or None,
            "timeout": self.connection.timeout_s,
            "banner_timeout": self.connection.timeout_s,
            "auth_timeout": self.connection.timeout_s,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if self.connection.auth_type == AuthType.KEY:
            arguments["key_filename"] = self.connection.key_path
            arguments["passphrase"] = self.secret
        else:
            arguments["password"] = self.secret
        client.connect(**arguments)
        self._ssh = client
        self._sftp = client.open_sftp()

    def close(self) -> None:
        if not self._owns_clients:
            return
        sftp, ssh = self._sftp, self._ssh
        self._sftp = None
        self._ssh = None
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        sftp = self._require_client()
        files: list[RemoteFile] = []
        for root in remote_paths:
            self._walk(
                sftp,
                _normalize(root),
                recursive=recursive,
                max_depth=max_depth,
                depth=0,
                output=files,
            )
        return ListingResult(tuple(files))

    def stat(self, remote_path: str) -> RemoteFile:
        sftp = self._require_client()
        path = _normalize(remote_path)
        attributes = sftp.lstat(path)
        return _attributes_to_file(path, attributes)

    def download_to(
        self,
        remote_path: str,
        target: BinaryIO,
        *,
        offset: int,
        block_size: int,
        on_chunk: Callable[[bytes], None],
        on_restart: Callable[[], None],
    ) -> TransferResult:
        sftp = self._require_client()
        path = _normalize(remote_path)
        bytes_received = 0
        resumed_from = offset
        resume_supported = True
        with sftp.open(path, "rb") as remote:
            if offset:
                try:
                    remote.seek(offset)
                except Exception:
                    target.seek(0)
                    target.truncate(0)
                    on_restart()
                    resumed_from = 0
                    resume_supported = False
                    remote.seek(0)
            while True:
                chunk = remote.read(block_size)
                if not chunk:
                    break
                on_chunk(chunk)
                written = target.write(chunk)
                if written != len(chunk):
                    raise OSError("No fue posible escribir el bloque SFTP completo.")
                bytes_received += len(chunk)
        return TransferResult(bytes_received, resumed_from, resume_supported)

    def _walk(
        self,
        sftp: paramiko.SFTPClient,
        path: str,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        output: list[RemoteFile],
    ) -> None:
        for attributes in sftp.listdir_attr(path):
            name = attributes.filename
            if name in {".", ".."}:
                continue
            remote_path = posixpath.join(path.rstrip("/"), name) or "/"
            mode = attributes.st_mode or 0
            if stat_module.S_ISLNK(mode):
                output.append(_attributes_to_file(remote_path, attributes))
                continue
            if stat_module.S_ISDIR(mode):
                if recursive and depth < max_depth:
                    self._walk(
                        sftp,
                        remote_path,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth + 1,
                        output=output,
                    )
                continue
            if stat_module.S_ISREG(mode) or mode == 0:
                output.append(_attributes_to_file(remote_path, attributes))

    def _require_client(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RuntimeError("La sesión SFTP no está conectada.")
        return self._sftp


def _attributes_to_file(
    remote_path: str, attributes: paramiko.SFTPAttributes
) -> RemoteFile:
    modified = (
        datetime.fromtimestamp(attributes.st_mtime, tz=timezone.utc)
        if attributes.st_mtime is not None
        else None
    )
    return RemoteFile(
        remote_path,
        attributes.st_size,
        modified,
        timestamp_reliable=modified is not None,
        timestamp_source="st_mtime",
        is_symlink=stat_module.S_ISLNK(attributes.st_mode or 0),
    )


def _normalize(value: str) -> str:
    normalized = "/" + value.strip().replace("\\", "/").lstrip("/")
    return normalized.rstrip("/") or "/"
