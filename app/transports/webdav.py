"""WebDAV/WebDAVS PROPFIND metadata listing."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import re
from typing import BinaryIO, Callable
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from app.errors import ErrorType, RecolectaError
from app.models import Connection, Protocol
from app.transports.base import (
    DirectoryWorkQueue,
    RemoteFile,
    TransferResult,
    Transport,
)


_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
  </d:prop>
</d:propfind>"""
_CONTENT_RANGE = re.compile(
    r"^bytes\s+(?P<start>\d+)-(?P<end>\d+)/(?P<total>\d+|\*)$",
    re.IGNORECASE,
)


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
        self._expected_scheme = (
            "https"
            if self.connection.protocol == Protocol.WEBDAVS
            else "http"
        )
        origin, self._base_path = _parse_endpoint(
            self.connection.host,
            expected_scheme=self._expected_scheme,
            configured_port=self.connection.port,
        )
        self._base_url = origin + quote(self._base_path, safe="/")

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
            and self.connection.ssl_mode == "insecure"
        )
        self._client = httpx.Client(
            auth=auth,
            timeout=self.connection.timeout_s,
            verify=verify,
            follow_redirects=True,
            event_hooks={"request": [self._validate_request_scheme]},
        )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            client.close()

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
        roots = ((_normalize_path(root), 0) for root in remote_paths)
        with DirectoryWorkQueue(roots) as directories:
            while True:
                work = directories.pop()
                if work is None:
                    return
                path, depth = work
                self._report_listing_location(path, depth)
                yield from self._walk(
                    path,
                    recursive=recursive,
                    max_depth=max_depth,
                    depth=depth,
                    directories=directories,
                )

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
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        bytes_received = 0
        resumed_from = offset
        resume_supported = True
        with client.stream("GET", self._url(path), headers=headers) as response:
            self._validate_response_scheme(response)
            if offset and response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = _CONTENT_RANGE.fullmatch(content_range.strip())
                if (
                    match is None
                    or int(match.group("start")) != offset
                    or int(match.group("end")) < offset
                ):
                    raise RuntimeError(
                        "WebDAV no confirmó el rango exacto solicitado; "
                        "el parcial no se modificó."
                    )
            elif offset and response.status_code == 200:
                target.seek(0)
                target.truncate(0)
                on_restart()
                resumed_from = 0
                resume_supported = False
            else:
                response.raise_for_status()
            for chunk in response.iter_raw(block_size):
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
        directories: DirectoryWorkQueue,
    ) -> Iterator[RemoteFile]:
        entries = self._propfind(path, depth="1")
        entries_seen = 0
        for entry_number, entry in enumerate(entries, start=1):
            entries_seen = entry_number
            if entry_number % 100 == 0:
                self._report_listing_location(
                    path,
                    depth,
                    count_location=False,
                    entries_delta=100,
                )
            if _same_resource(entry.path, path):
                if not entry.is_directory and entry.file is not None:
                    yield entry.file
                continue
            if entry.is_directory:
                if recursive and depth < max_depth:
                    directories.add(entry.path, depth + 1)
            elif entry.file is not None:
                yield entry.file
        if entries_seen % 100:
            self._report_listing_location(
                path,
                depth,
                count_location=False,
                entries_delta=entries_seen % 100,
            )

    def _propfind(
        self,
        path: str,
        *,
        depth: str,
    ) -> Iterator[_DavEntry]:
        client = self._require_client()
        with client.stream(
            "PROPFIND",
            self._url(path),
            headers={
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
            },
            content=_PROPFIND_BODY,
        ) as response:
            self._validate_response_scheme(response)
            if response.status_code != 207:
                response.raise_for_status()
                raise RuntimeError(
                    f"WebDAV devolvió HTTP {response.status_code}; se esperaba 207."
                )
            parser = ElementTree.XMLPullParser(events=("start", "end"))
            document_root = None
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                parser.feed(chunk)
                for event, node in parser.read_events():
                    if event == "start" and document_root is None:
                        document_root = node
                        continue
                    if event != "end" or node.tag != "{DAV:}response":
                        continue
                    entry = _dav_entry(node, base_path=self._base_path)
                    if document_root is not None:
                        try:
                            document_root.remove(node)
                        except ValueError:
                            pass
                    node.clear()
                    if entry is not None:
                        yield entry

    def _url(self, path: str) -> str:
        return self._base_url + quote(path, safe="/")

    def _validate_request_scheme(self, request: httpx.Request) -> None:
        if (
            self._expected_scheme == "https"
            and request.url.scheme.lower() != "https"
        ):
            raise RecolectaError(
                ErrorType.TLS,
                "WebDAVS rechazó una redirección a HTTP sin cifrado.",
            )

    def _validate_response_scheme(self, response: httpx.Response) -> None:
        if (
            self._expected_scheme == "https"
            and response.url.scheme.lower() != "https"
        ):
            raise RecolectaError(
                ErrorType.TLS,
                "WebDAVS recibió una respuesta mediante HTTP sin cifrado.",
            )

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("La sesión WebDAV no está conectada.")
        return self._client


def _dav_entry(
    response_node: ElementTree.Element,
    *,
    base_path: str = "",
) -> _DavEntry | None:
    href_node = response_node.find("{DAV:}href")
    if href_node is None or not href_node.text:
        return None
    href_path = unquote(urlsplit(href_node.text).path)
    remote_path = _logical_remote_path(href_path, base_path=base_path)
    prop = response_node.find(".//{DAV:}prop")
    if prop is None:
        return None
    resource_type = prop.find("{DAV:}resourcetype")
    is_directory = (
        resource_type is not None
        and resource_type.find("{DAV:}collection") is not None
    )
    if is_directory:
        return _DavEntry(remote_path, True, None)
    size_node = prop.find("{DAV:}getcontentlength")
    modified_node = prop.find("{DAV:}getlastmodified")
    size = _optional_int(
        size_node.text if size_node is not None else None
    )
    modified = None
    if modified_node is not None and modified_node.text:
        modified = parsedate_to_datetime(modified_node.text)
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        modified = modified.astimezone(timezone.utc)
    return _DavEntry(
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


def _normalize_path(value: str) -> str:
    normalized = "/" + value.strip().replace("\\", "/").lstrip("/")
    return normalized.rstrip("/") or "/"


def _parse_endpoint(
    host: str,
    *,
    expected_scheme: str,
    configured_port: int | None,
) -> tuple[str, str]:
    """Build a strict origin while preserving an optional DAV base path."""
    raw = host.strip()
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    if parsed.scheme and parsed.scheme.lower() != expected_scheme:
        raise ValueError(
            f"El protocolo requiere {expected_scheme}:// y el host usa "
            f"{parsed.scheme.lower()}://."
        )
    if not parsed.hostname:
        raise ValueError("El host WebDAV no contiene un servidor válido.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "No incluya credenciales en el host WebDAV; use los campos de usuario."
        )
    if parsed.query or parsed.fragment:
        raise ValueError("El host WebDAV no admite consulta ni fragmento URL.")
    try:
        embedded_port = parsed.port
    except ValueError as exc:
        raise ValueError("El puerto incluido en el host WebDAV no es válido.") from exc
    default_port = 443 if expected_scheme == "https" else 80
    port = embedded_port or configured_port or default_port
    hostname = parsed.hostname
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port != default_port:
        authority = f"{authority}:{port}"
    path = unquote(parsed.path or "")
    base_path = _normalize_path(path) if path and path != "/" else ""
    return f"{expected_scheme}://{authority}", base_path


def _logical_remote_path(href_path: str, *, base_path: str) -> str:
    normalized = _normalize_path(href_path)
    if not base_path:
        return normalized
    normalized_base = _normalize_path(base_path)
    if normalized == normalized_base:
        return "/"
    prefix = normalized_base.rstrip("/") + "/"
    if normalized.startswith(prefix):
        return _normalize_path(normalized[len(normalized_base) :])
    return normalized


def _same_resource(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
