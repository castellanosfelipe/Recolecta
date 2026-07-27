"""WebDAV/WebDAVS PROPFIND metadata listing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import BinaryIO, Callable
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from app.models import Connection, Protocol
from app.transports.base import ListingResult, RemoteFile, TransferResult, Transport


_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>"""


@dataclass(frozen=True)
class _DavEntry:
    path: str
    is_directory: bool
    file: RemoteFile | None


class WebDavTransport(Transport):
    """List WebDAV resources using standard DAV properties."""

    def __init__(
        self,
        connection: Connection,
        *,
        secret: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        self.connection = connection.normalized()
        self.secret = secret
        self._client = client
        self._owns_client = client is None
        scheme = (
            "https"
            if self.connection.protocol == Protocol.WEBDAVS
            else "http"
        )
        host = self.connection.host.rstrip("/")
        if "://" in host:
            parsed = urlsplit(host)
            scheme = parsed.scheme
            authority = parsed.netloc
        else:
            authority = host
        default_port = 443 if scheme == "https" else 80
        if self.connection.port and self.connection.port != default_port and ":" not in authority:
            authority = f"{authority}:{self.connection.port}"
        self._base_url = f"{scheme}://{authority}"

    def connect(self) -> None:
        if self._client is not None:
            return
        auth = (
            (self.connection.username, self.secret or "")
            if self.connection.username
            else None
        )
        verify = not (
            self.connection.protocol == Protocol.WEBDAVS
            and self.connection.ssl_mode != "required"
        )
        self._client = httpx.Client(
            auth=auth,
            timeout=self.connection.timeout_s,
            verify=verify,
            follow_redirects=True,
        )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            client.close()

    def list_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ) -> ListingResult:
        files: list[RemoteFile] = []
        visited: set[str] = set()
        for root in remote_paths:
            self._walk(
                _normalize_path(root),
                recursive=recursive,
                max_depth=max_depth,
                depth=0,
                visited=visited,
                output=files,
            )
        return ListingResult(tuple(files))

    def stat(self, remote_path: str) -> RemoteFile:
        path = _normalize_path(remote_path)
        entries = self._propfind(path, depth="0")
        for entry in entries:
            if not entry.is_directory and entry.file is not None:
                return entry.file
        raise FileNotFoundError(f"No existe el archivo WebDAV {path}.")

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
        client = self._require_client()
        path = _normalize_path(remote_path)
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        bytes_received = 0
        resumed_from = offset
        resume_supported = True
        with client.stream("GET", self._url(path), headers=headers) as response:
            if offset and response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                if content_range and not content_range.startswith(f"bytes {offset}-"):
                    raise RuntimeError(
                        "WebDAV devolvió un Content-Range distinto del solicitado."
                    )
            elif offset and response.status_code == 200:
                target.seek(0)
                target.truncate(0)
                on_restart()
                resumed_from = 0
                resume_supported = False
            else:
                response.raise_for_status()
            for chunk in response.iter_bytes(block_size):
                if not chunk:
                    continue
                on_chunk(chunk)
                written = target.write(chunk)
                if written != len(chunk):
                    raise OSError(
                        "No fue posible escribir el bloque WebDAV completo."
                    )
                bytes_received += len(chunk)
        return TransferResult(bytes_received, resumed_from, resume_supported)

    def _walk(
        self,
        path: str,
        *,
        recursive: bool,
        max_depth: int,
        depth: int,
        visited: set[str],
        output: list[RemoteFile],
    ) -> None:
        if path in visited:
            return
        visited.add(path)
        entries = self._propfind(path, depth="1")
        for entry in entries:
            if _same_resource(entry.path, path):
                if not entry.is_directory and entry.file is not None:
                    output.append(entry.file)
                continue
            if entry.is_directory:
                if recursive and depth < max_depth:
                    self._walk(
                        entry.path,
                        recursive=recursive,
                        max_depth=max_depth,
                        depth=depth + 1,
                        visited=visited,
                        output=output,
                    )
            elif entry.file is not None:
                output.append(entry.file)

    def _propfind(self, path: str, *, depth: str) -> tuple[_DavEntry, ...]:
        client = self._require_client()
        response = client.request(
            "PROPFIND",
            self._url(path),
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=_PROPFIND_BODY,
        )
        if response.status_code != 207:
            response.raise_for_status()
            raise RuntimeError(
                f"WebDAV devolvió HTTP {response.status_code}; se esperaba 207."
            )
        root = ElementTree.fromstring(response.content)
        entries: list[_DavEntry] = []
        for response_node in root.findall(".//{DAV:}response"):
            href_node = response_node.find("{DAV:}href")
            if href_node is None or not href_node.text:
                continue
            remote_path = _normalize_path(
                unquote(urlsplit(href_node.text).path)
            )
            prop = response_node.find(".//{DAV:}prop")
            if prop is None:
                continue
            resource_type = prop.find("{DAV:}resourcetype")
            is_directory = (
                resource_type is not None
                and resource_type.find("{DAV:}collection") is not None
            )
            if is_directory:
                entries.append(_DavEntry(remote_path, True, None))
                continue
            size_node = prop.find("{DAV:}getcontentlength")
            modified_node = prop.find("{DAV:}getlastmodified")
            size = _optional_int(size_node.text if size_node is not None else None)
            modified = None
            if modified_node is not None and modified_node.text:
                modified = parsedate_to_datetime(modified_node.text)
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=timezone.utc)
                modified = modified.astimezone(timezone.utc)
            entries.append(
                _DavEntry(
                    remote_path,
                    False,
                    RemoteFile(
                        remote_path,
                        size,
                        modified,
                        timestamp_reliable=modified is not None,
                        timestamp_source="getlastmodified",
                    ),
                )
            )
        return tuple(entries)

    def _url(self, path: str) -> str:
        return self._base_url + quote(path, safe="/")

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("La sesión WebDAV no está conectada.")
        return self._client


def _normalize_path(value: str) -> str:
    normalized = "/" + value.strip().replace("\\", "/").lstrip("/")
    return normalized.rstrip("/") or "/"


def _same_resource(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
