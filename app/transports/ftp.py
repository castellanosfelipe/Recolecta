"""FTP and explicit FTPS metadata listing."""

from __future__ import annotations

import ftplib
import posixpath
import re
import ssl
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable
from zoneinfo import ZoneInfo

from app.models import Connection, Protocol
from app.transports.base import ListingResult, RemoteFile, TransferResult, Transport


_UNIX_LIST_RE = re.compile(
    r"^(?P<mode>[bcdlps-][rwxStTs-]{9})\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<clock_year>\d{4}|\d{1,2}:\d{2})\s+(?P<name>.+)$"
)
_WINDOWS_LIST_RE = re.compile(
    r"^(?P<month>\d{2})-(?P<day>\d{2})-(?P<year>\d{2,4})\s+"
    r"(?P<hour>\d{2}):(?P<minute>\d{2})(?P<ampm>AM|PM)\s+"
    r"(?P<size_dir><DIR>|\d+)\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_MLSD_UNSUPPORTED = ("500", "501", "502", "504")


class FtpTransport(Transport):
    """List FTP/FTPS using MDTM first, MLSD second, and LIST as fallback."""

    def __init__(
        self,
        connection: Connection,
        *,
        secret: str | None,
        client: ftplib.FTP | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection = connection.normalized()
        self.secret = secret or ""
        self._ftp = client
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(timezone.utc))

    def connect(self) -> None:
        if self._ftp is not None:
            return
        if self.connection.protocol == Protocol.FTPS:
            context = ssl.create_default_context()
            if self.connection.ssl_mode != "required":
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            ftp: ftplib.FTP = ftplib.FTP_TLS(context=context)
        else:
            ftp = ftplib.FTP()
        self._ftp = ftp
        try:
            ftp.connect(
                self.connection.host,
                self.connection.port or 21,
                timeout=self.connection.timeout_s,
            )
            ftp.login(self.connection.username, self.secret)
            ftp.set_pasv(True)
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        ftp, self._ftp = self._ftp, None
        if ftp is None or not self._owns_client:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        ftp = self._require_client()
        files: list[RemoteFile] = []
        warnings: list[str] = []
        for configured_path in remote_paths:
            root = _normalize_remote_path(configured_path)
            root_files: list[RemoteFile] = []
            try:
                self._walk_mlsd(
                    ftp,
                    root,
                    recursive=recursive,
                    max_depth=max_depth,
                    depth=0,
                    output=root_files,
                )
            except (AttributeError, ftplib.error_perm) as exc:
                if isinstance(exc, ftplib.error_perm) and not str(exc).startswith(
                    _MLSD_UNSUPPORTED
                ):
                    raise
                warning = (
                    "El servidor FTP no soporta MLSD/MDTM de forma utilizable; "
                    "se usó LIST con precisión temporal limitada."
                )
                if warning not in warnings:
                    warnings.append(warning)
                root_files.clear()
                self._walk_list(
                    ftp,
                    root,
                    recursive=recursive,
                    max_depth=max_depth,
                    depth=0,
                    output=root_files,
                )
            files.extend(root_files)
        return ListingResult(tuple(files), tuple(warnings))

    def stat(self, remote_path: str) -> RemoteFile:
        ftp = self._require_client()
        path = _normalize_remote_path(remote_path)
        size: int | None
        try:
            ftp.voidcmd("TYPE I")
            size = ftp.size(path)
        except ftplib.all_errors:
            size = None
        modified = self._mdtm(ftp, path)
        if modified is not None:
            return RemoteFile(path, size, modified, True, "MDTM")
        parent, name = posixpath.split(path)
        result = self.list_files((parent or "/",), recursive=False, max_depth=0)
        for item in result.files:
            if item.name == name:
                return item
        raise FileNotFoundError(f"No existe el archivo remoto {path}.")

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
        ftp = self._require_client()
        path = _normalize_remote_path(remote_path)
        bytes_received = 0

        def write_chunk(chunk: bytes) -> None:
            nonlocal bytes_received
            on_chunk(chunk)
            written = target.write(chunk)
            if written != len(chunk):
                raise OSError("No fue posible escribir el bloque FTP completo.")
            bytes_received += len(chunk)

        if offset > 0:
            try:
                ftp.retrbinary(
                    f"RETR {path}",
                    write_chunk,
                    blocksize=block_size,
                    rest=offset,
                )
                return TransferResult(bytes_received, offset, True)
            except ftplib.error_perm as exc:
                if not str(exc).startswith(_MLSD_UNSUPPORTED):
                    raise
                target.seek(0)
                target.truncate(0)
                on_restart()
                bytes_received = 0
                ftp.retrbinary(
                    f"RETR {path}",
                    write_chunk,
                    blocksize=block_size,
                )
                return TransferResult(bytes_received, 0, False)

        ftp.retrbinary(
            f"RETR {path}",
            write_chunk,
            blocksize=block_size,
        )
        return TransferResult(bytes_received, 0, True)

    def _walk_mlsd(
        self,
        ftp: ftplib.FTP,
        path: str,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        output: list[RemoteFile],
    ) -> None:
        entries = list(ftp.mlsd(path, facts=["type", "size", "modify"]))
        for name, facts in entries:
            if name in {".", ".."}:
                continue
            remote_path = posixpath.join(path.rstrip("/"), name) or "/"
            entry_type = facts.get("type", "").lower()
            if entry_type in {"cdir", "pdir"}:
                continue
            is_symlink = "slink" in entry_type
            if entry_type == "dir":
                if recursive and depth < max_depth:
                    self._walk_mlsd(
                        ftp,
                        remote_path,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth + 1,
                        output=output,
                    )
                continue
            if entry_type not in {"file"} and not is_symlink:
                continue
            size = _parse_int(facts.get("size"))
            modified = self._mdtm(ftp, remote_path)
            source = "MDTM"
            if modified is None:
                modified = _parse_mlsd_timestamp(facts.get("modify"))
                source = "MLSD"
            output.append(
                RemoteFile(
                    remote_path,
                    size,
                    modified,
                    timestamp_reliable=modified is not None,
                    timestamp_source=source,
                    is_symlink=is_symlink,
                )
            )

    def _walk_list(
        self,
        ftp: ftplib.FTP,
        path: str,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        output: list[RemoteFile],
    ) -> None:
        lines: list[str] = []
        ftp.retrlines(f"LIST {path}", lines.append)
        for line in lines:
            parsed = _parse_list_line(
                line,
                server_zone=ZoneInfo(self.connection.timezone),
                now=self._now(),
            )
            if parsed is None:
                continue
            name, is_dir, is_symlink, size, modified = parsed
            remote_path = posixpath.join(path.rstrip("/"), name) or "/"
            if is_dir:
                if recursive and not is_symlink and depth < max_depth:
                    self._walk_list(
                        ftp,
                        remote_path,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth + 1,
                        output=output,
                    )
                continue
            output.append(
                RemoteFile(
                    remote_path,
                    size,
                    modified,
                    timestamp_reliable=False,
                    timestamp_source="LIST",
                    is_symlink=is_symlink,
                )
            )

    @staticmethod
    def _mdtm(ftp: ftplib.FTP, remote_path: str) -> datetime | None:
        try:
            response = ftp.sendcmd(f"MDTM {remote_path}")
        except ftplib.all_errors:
            return None
        if not response.startswith("213 "):
            return None
        return _parse_mlsd_timestamp(response[4:].strip())

    def _require_client(self) -> ftplib.FTP:
        if self._ftp is None:
            raise RuntimeError("La sesión FTP no está conectada.")
        return self._ftp


def _normalize_remote_path(value: str) -> str:
    normalized = "/" + value.strip().replace("\\", "/").lstrip("/")
    return normalized.rstrip("/") or "/"


def _parse_mlsd_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.split(".", 1)[0]
    try:
        parsed = datetime.strptime(cleaned, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _parse_list_line(
    line: str,
    *,
    server_zone: ZoneInfo,
    now: datetime,
) -> tuple[str, bool, bool, int | None, datetime | None] | None:
    unix = _UNIX_LIST_RE.match(line)
    if unix:
        mode = unix.group("mode")
        name = unix.group("name")
        if mode.startswith("l") and " -> " in name:
            name = name.split(" -> ", 1)[0]
        month = datetime.strptime(unix.group("month"), "%b").month
        day = int(unix.group("day"))
        clock_year = unix.group("clock_year")
        now_local = now.astimezone(server_zone)
        if ":" in clock_year:
            hour, minute = (int(part) for part in clock_year.split(":"))
            year = now_local.year
            local = datetime(year, month, day, hour, minute, tzinfo=server_zone)
            if local > now_local + timedelta(days=1):
                local = local.replace(year=year - 1)
        else:
            local = datetime(int(clock_year), month, day, tzinfo=server_zone)
        return (
            name,
            mode.startswith("d"),
            mode.startswith("l"),
            int(unix.group("size")),
            local.astimezone(timezone.utc),
        )

    windows = _WINDOWS_LIST_RE.match(line)
    if windows:
        year = int(windows.group("year"))
        if year < 100:
            year += 2000 if year < 70 else 1900
        hour = int(windows.group("hour")) % 12
        if windows.group("ampm").upper() == "PM":
            hour += 12
        local = datetime(
            year,
            int(windows.group("month")),
            int(windows.group("day")),
            hour,
            int(windows.group("minute")),
            tzinfo=server_zone,
        )
        size_dir = windows.group("size_dir")
        return (
            windows.group("name"),
            size_dir.upper() == "<DIR>",
            False,
            None if size_dir.upper() == "<DIR>" else int(size_dir),
            local.astimezone(timezone.utc),
        )
    return None
