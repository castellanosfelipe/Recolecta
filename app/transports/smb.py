"""SMB2/SMB3 transport with bounded network operations and local test paths."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from app.models import Connection
from app.transports.base import (
    DirectoryWorkQueue,
    RemoteFile,
    TransferResult,
    Transport,
)


class SmbTransport(Transport):
    """Use smbprotocol for UNC paths and pathlib only for local development."""

    def __init__(self, connection: Connection, *, secret: str | None) -> None:
        self.connection = connection.normalized()
        self.secret = secret
        self._connection_cache: dict[str, object] = {}
        self._smb_client = None

    def connect(self) -> None:
        if not any(self._is_unc(self._resolve(path)) for path in self.connection.remote_paths):
            return
        smbclient = self._client()
        port = self.connection.port or 445
        connection_key = f"{self.connection.host.lower()}:{port}"
        protocol_connection = self._connection_cache.get(connection_key)
        if not _connection_is_open(protocol_connection):
            protocol_connection = _new_protocol_connection(
                smbclient,
                self.connection.host,
                port,
            )
            self._install_operation_timeout(protocol_connection)
            self._connection_cache[connection_key] = protocol_connection
            try:
                protocol_connection.connect(timeout=self.connection.timeout_s)
            except BaseException:
                self.close()
                raise
        else:
            self._install_operation_timeout(protocol_connection)
        try:
            session = smbclient.register_session(
                self.connection.host,
                username=self.connection.username or None,
                password=self.secret,
                port=port,
                connection_timeout=self.connection.timeout_s,
                connection_cache=self._connection_cache,
            )
            self._install_operation_timeout(session.connection)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._smb_client is None:
            self._connection_cache.clear()
            return
        try:
            self._smb_client.reset_connection_cache(
                fail_on_error=False,
                connection_cache=self._connection_cache,
            )
        finally:
            self._connection_cache.clear()

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> Iterator[RemoteFile]:
        self._reset_listing_warnings()
        return self._iter_roots(
            remote_paths,
            recursive=recursive,
            max_depth=max_depth,
        )

    def _iter_roots(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> Iterator[RemoteFile]:
        roots = ((self._resolve(configured), 0) for configured in remote_paths)
        with DirectoryWorkQueue(roots) as directories:
            while True:
                work = directories.pop()
                if work is None:
                    return
                raw_path, depth = work
                self._report_listing_location(raw_path, depth)
                if self._is_unc(raw_path):
                    metadata = self._remote_metadata(raw_path)
                    if metadata.is_symlink or metadata.size_bytes is not None:
                        yield metadata
                        continue
                    yield from self._walk_remote(
                        raw_path,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth,
                        directories=directories,
                    )
                    continue
                root = Path(raw_path)
                if root.is_file() or root.is_symlink():
                    yield _path_to_remote_file(root)
                    continue
                yield from self._walk(
                    root,
                    recursive=recursive,
                    max_depth=max_depth,
                    depth=depth,
                    directories=directories,
                )

    def stat(self, remote_path: str) -> RemoteFile:
        path = self._resolve(remote_path)
        if self._is_unc(path):
            return self._remote_metadata(path)
        local = Path(path)
        if not local.exists() and not local.is_symlink():
            raise FileNotFoundError(f"No existe el archivo SMB {local}.")
        return _path_to_remote_file(local)

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
        path = self._resolve(remote_path)
        bytes_received = 0
        if self._is_unc(path):
            remote_context = self._client().open_file(
                path,
                mode="rb",
                buffering=0,
                **self._operation_kwargs(),
            )
        else:
            remote_context = Path(path).open("rb")
        with remote_context as remote:
            remote.seek(offset)
            while True:
                chunk = remote.read(block_size)
                if not chunk:
                    break
                on_chunk(chunk)
                written = target.write(chunk)
                if written != len(chunk):
                    raise OSError("No fue posible escribir el bloque SMB completo.")
                bytes_received += len(chunk)
        return TransferResult(bytes_received, offset, True)

    def _walk(
        self,
        directory: Path,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        directories: DirectoryWorkQueue,
    ) -> Iterator[RemoteFile]:
        entries_seen = 0
        with os.scandir(directory) as entries:
            for entry_number, directory_entry in enumerate(entries, start=1):
                entries_seen = entry_number
                if entry_number % 100 == 0:
                    self._report_listing_location(
                        str(directory),
                        depth,
                        count_location=False,
                        entries_delta=100,
                    )
                entry = Path(directory_entry.path)
                if directory_entry.is_symlink():
                    yield _path_to_remote_file(entry)
                    continue
                if directory_entry.is_dir(follow_symlinks=False):
                    if recursive and depth < max_depth:
                        directories.add(str(entry), depth + 1)
                    continue
                if directory_entry.is_file(follow_symlinks=False):
                    yield _path_to_remote_file(entry)
        if entries_seen % 100:
            self._report_listing_location(
                str(directory),
                depth,
                count_location=False,
                entries_delta=entries_seen % 100,
            )

    def _walk_remote(
        self,
        directory: str,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        directories: DirectoryWorkQueue,
    ) -> Iterator[RemoteFile]:
        entries_seen = 0
        with self._client().scandir(
            directory,
            **self._operation_kwargs(),
        ) as entries:
            for entry_number, entry in enumerate(entries, start=1):
                entries_seen = entry_number
                if entry_number % 100 == 0:
                    self._report_listing_location(
                        directory,
                        depth,
                        count_location=False,
                        entries_delta=100,
                    )
                path = str(entry.path)
                if entry.is_symlink():
                    yield self._metadata_from_stat(
                        path,
                        entry.stat(follow_symlinks=False),
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive and depth < max_depth:
                        directories.add(path, depth + 1)
                    continue
                if entry.is_file(follow_symlinks=False):
                    yield self._metadata_from_stat(
                        path,
                        entry.stat(follow_symlinks=False),
                    )
        if entries_seen % 100:
            self._report_listing_location(
                directory,
                depth,
                count_location=False,
                entries_delta=entries_seen % 100,
            )

    def _resolve(self, remote_path: str) -> str:
        value = remote_path.strip()
        if value.startswith("\\\\"):
            server = value[2:].split("\\", 1)[0]
            if server.casefold() != self.connection.host.casefold():
                raise ValueError(
                    "La ruta UNC SMB debe pertenecer al host configurado "
                    f"{self.connection.host!r}."
                )
            return value
        local_path = Path(value)
        if local_path.exists() or local_path.is_symlink() or local_path.drive:
            return value
        relative = value.replace("/", "\\").lstrip("\\")
        return f"\\\\{self.connection.host}\\{relative}"

    @staticmethod
    def _is_unc(path: str) -> bool:
        return path.startswith("\\\\")

    def _client(self):
        if self._smb_client is None:
            import smbclient

            self._smb_client = smbclient
        return self._smb_client

    def _operation_kwargs(self) -> dict[str, object]:
        return {
            "username": self.connection.username or None,
            "password": self.secret,
            "port": self.connection.port or 445,
            "connection_timeout": self.connection.timeout_s,
            "connection_cache": self._connection_cache,
        }

    def _remote_metadata(self, path: str) -> RemoteFile:
        metadata = self._client().stat(
            path,
            follow_symlinks=False,
            **self._operation_kwargs(),
        )
        return self._metadata_from_stat(path, metadata)

    @staticmethod
    def _metadata_from_stat(path: str, metadata) -> RemoteFile:
        mode = metadata.st_mode
        return RemoteFile(
            path,
            None if stat.S_ISDIR(mode) else metadata.st_size,
            datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc),
            timestamp_reliable=True,
            timestamp_source="SMB st_mtime",
            is_symlink=stat.S_ISLNK(mode),
        )

    def _install_operation_timeout(self, connection) -> None:
        """Apply the configured timeout to every SMB response wait.

        ``smbclient`` exposes ``connection_timeout`` for the TCP handshake but
        leaves individual SMB request waits unbounded.  Its high-level API
        funnels those waits through ``Connection.receive``; wrapping the
        per-transport connection keeps list/stat/read operations bounded while
        preserving any shorter timeout explicitly supplied by the library.
        """

        if getattr(connection, "_recolecta_receive_wrapped", False):
            return
        original_receive = connection.receive
        operation_timeout = self.connection.timeout_s

        def receive(
            request,
            wait=True,
            timeout=None,
            resolve_symlinks=True,
        ):
            effective_timeout = (
                operation_timeout
                if timeout is None
                else min(operation_timeout, timeout)
            )
            return original_receive(
                request,
                wait=wait,
                timeout=effective_timeout,
                resolve_symlinks=resolve_symlinks,
            )

        connection.receive = receive
        connection._recolecta_receive_wrapped = True


def _path_to_remote_file(path: Path) -> RemoteFile:
    is_symlink = path.is_symlink()
    metadata = path.lstat() if is_symlink else path.stat()
    return RemoteFile(
        str(path),
        None if path.is_dir() else metadata.st_size,
        datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc),
        timestamp_reliable=True,
        timestamp_source="st_mtime",
        is_symlink=is_symlink,
    )


def _new_protocol_connection(smbclient, host: str, port: int):
    """Create a signed SMB connection before authentication starts.

    Pre-registering the connection lets Recolecta install its receive timeout
    before ``Session.connect`` performs the credential exchange.
    """

    from smbprotocol.connection import Connection as ProtocolConnection

    return ProtocolConnection(
        smbclient.ClientConfig().client_guid,
        host,
        port,
        require_signing=True,
    )


def _connection_is_open(connection: object | None) -> bool:
    transport = getattr(connection, "transport", None)
    return bool(connection is not None and getattr(transport, "connected", False))
