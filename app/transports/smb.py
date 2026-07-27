"""SMB/UNC metadata listing with optional explicit Windows credentials."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.models import Connection
from app.transports.base import ListingResult, RemoteFile, Transport


_UNC_SHARE = re.compile(r"^(\\\\[^\\]+\\[^\\]+)")


class SmbTransport(Transport):
    """List UNC or local development paths through pathlib."""

    def __init__(self, connection: Connection, *, secret: str | None) -> None:
        self.connection = connection.normalized()
        self.secret = secret
        self._connected_shares: set[str] = set()

    def connect(self) -> None:
        return None

    def close(self) -> None:
        if sys.platform != "win32":
            self._connected_shares.clear()
            return
        try:
            import win32wnet

            for share in tuple(self._connected_shares):
                try:
                    win32wnet.WNetCancelConnection2(share, 0, False)
                except Exception:
                    pass
        finally:
            self._connected_shares.clear()

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        files: list[RemoteFile] = []
        for configured in remote_paths:
            root = self._resolve(configured)
            self._ensure_credentials(root)
            if root.is_file() or root.is_symlink():
                files.append(_path_to_remote_file(root))
                continue
            self._walk(
                root,
                recursive=recursive,
                max_depth=max_depth,
                depth=0,
                output=files,
            )
        return ListingResult(tuple(files))

    def stat(self, remote_path: str) -> RemoteFile:
        path = self._resolve(remote_path)
        self._ensure_credentials(path)
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(f"No existe el archivo SMB {path}.")
        return _path_to_remote_file(path)

    def _walk(
        self,
        directory: Path,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        output: list[RemoteFile],
    ) -> None:
        for entry in directory.iterdir():
            if entry.is_symlink():
                output.append(_path_to_remote_file(entry))
                continue
            if entry.is_dir():
                if recursive and depth < max_depth:
                    self._walk(
                        entry,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth + 1,
                        output=output,
                    )
                continue
            if entry.is_file():
                output.append(_path_to_remote_file(entry))

    def _resolve(self, remote_path: str) -> Path:
        value = remote_path.strip()
        if value.startswith("\\\\") or Path(value).is_absolute():
            return Path(value)
        relative = value.replace("/", "\\").lstrip("\\")
        return Path(f"\\\\{self.connection.host}\\{relative}")

    def _ensure_credentials(self, path: Path) -> None:
        match = _UNC_SHARE.match(str(path))
        if match is None or not self.connection.username:
            return
        share = match.group(1)
        if share in self._connected_shares:
            return
        if sys.platform != "win32":
            raise RuntimeError("Las credenciales SMB explícitas requieren Windows.")
        import win32netcon
        import win32wnet

        resource = win32wnet.NETRESOURCE()
        resource.dwType = win32netcon.RESOURCETYPE_DISK
        resource.lpRemoteName = share
        win32wnet.WNetAddConnection2(
            resource,
            self.secret or "",
            self.connection.username,
            0,
        )
        self._connected_shares.add(share)


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
