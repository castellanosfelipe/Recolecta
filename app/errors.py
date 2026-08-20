"""Central error taxonomy and exception classification."""

from __future__ import annotations

import errno
import ftplib
import socket
import ssl
from enum import StrEnum


class ErrorType(StrEnum):
    """Stable error identifiers persisted in runs and exports."""

    DNS = "dns"
    TCP_CONNECT = "tcp_connect"
    TCP_TIMEOUT = "tcp_timeout"
    AUTH = "auth"
    TLS = "tls"
    PERMISSION = "permission"
    TARGET_MISSING = "target_missing"
    PROTOCOL = "protocol"
    DISK_SPACE = "disk_space"
    DISK_WRITE = "disk_write"
    INTEGRITY = "integrity"
    PARTIAL_TRANSFER = "partial_transfer"
    PATH_INVALID = "path_invalid"
    INTERRUPTED = "interrupted"
    TIMESTAMP_UNRELIABLE = "timestamp_unreliable"
    UNKNOWN = "unknown"


class RecolectaError(Exception):
    """Actionable application error with a stable category."""

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


def classify_exception(exc: BaseException) -> ErrorType:
    """Map common Python/network exceptions to the public taxonomy."""
    return _classify_exception(exc, visited=set())


def _classify_exception(
    exc: BaseException,
    *,
    visited: set[int],
) -> ErrorType:
    """Classify one exception and safely inspect its wrapped failures."""
    identity = id(exc)
    if identity in visited:
        return ErrorType.UNKNOWN
    visited.add(identity)

    if isinstance(exc, RecolectaError):
        return exc.error_type
    winerror = getattr(exc, "winerror", None)
    if winerror in {86, 1219, 1326, 1327, 1330, 1331, 1909}:
        return ErrorType.AUTH
    if winerror == 5:
        return ErrorType.PERMISSION
    if winerror in {53, 67}:
        return ErrorType.TARGET_MISSING
    optional = _classify_optional_dependency_exception(
        exc,
        visited=visited,
    )
    if optional is not None:
        return optional
    if isinstance(exc, ftplib.error_perm):
        response = str(exc).lstrip()
        if response.startswith("530"):
            return ErrorType.AUTH
        if response.startswith("550"):
            detail = response[3:].casefold()
            missing_markers = (
                "not found",
                "does not exist",
                "doesn't exist",
                "no such file",
                "no such directory",
                "cannot find",
                "can't find",
            )
            if any(marker in detail for marker in missing_markers):
                return ErrorType.TARGET_MISSING
            permission_markers = (
                "access denied",
                "access is denied",
                "permission denied",
            )
            if any(marker in detail for marker in permission_markers):
                return ErrorType.PERMISSION
            # RFC 959 uses 550 for both a missing target and denied access.
            # Keep an otherwise ambiguous 550 as permission: this preserves
            # the historical classification without claiming that a remote
            # path is absent when the server intentionally hides it.
            return ErrorType.PERMISSION
        return ErrorType.PROTOCOL
    # RFC 959 reserves 4xx replies for temporary failures.  Treat them as a
    # retryable interrupted transfer (including the common 426 reply) instead
    # of a permanent protocol error.
    if isinstance(exc, ftplib.error_temp):
        response = str(exc).lstrip()
        if response.startswith(("421", "425")):
            return ErrorType.TCP_CONNECT
        return ErrorType.PARTIAL_TRANSFER
    if isinstance(exc, (ftplib.error_reply, ftplib.error_proto)):
        return ErrorType.PROTOCOL
    if isinstance(exc, ftplib.Error):
        return ErrorType.PROTOCOL
    if isinstance(exc, EOFError):
        return ErrorType.PARTIAL_TRANSFER
    if isinstance(exc, socket.gaierror):
        return ErrorType.DNS
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ErrorType.TCP_TIMEOUT
    if isinstance(exc, ssl.SSLError):
        return ErrorType.TLS
    if isinstance(exc, PermissionError):
        return ErrorType.PERMISSION
    if isinstance(exc, FileNotFoundError):
        return ErrorType.TARGET_MISSING
    if isinstance(exc, ConnectionRefusedError):
        return ErrorType.TCP_CONNECT
    if isinstance(exc, BrokenPipeError):
        return ErrorType.PARTIAL_TRANSFER
    if isinstance(exc, OSError):
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            return ErrorType.DISK_SPACE
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return ErrorType.PERMISSION
        if exc.errno in {
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
        }:
            return ErrorType.TCP_CONNECT
        if exc.errno == errno.EPIPE:
            return ErrorType.PARTIAL_TRANSFER
        if exc.errno in {errno.EIO, getattr(errno, "EROFS", -1)}:
            return ErrorType.DISK_WRITE
    wrapped = _visible_wrapped_exception(exc)
    if wrapped is not None and wrapped is not exc:
        wrapped_type = _classify_exception(wrapped, visited=visited)
        if wrapped_type != ErrorType.UNKNOWN:
            return wrapped_type
    return ErrorType.UNKNOWN


def is_retryable(error_type: ErrorType) -> bool:
    """Return whether the category is transient by default."""
    return error_type in {
        ErrorType.DNS,
        ErrorType.TCP_CONNECT,
        ErrorType.TCP_TIMEOUT,
        ErrorType.PARTIAL_TRANSFER,
        ErrorType.UNKNOWN,
    }


def _classify_optional_dependency_exception(
    exc: BaseException,
    *,
    visited: set[int],
) -> ErrorType | None:
    try:
        from smbprotocol import exceptions as smb_exceptions

        if isinstance(
            exc,
            (
                smb_exceptions.SMBAuthenticationError,
                smb_exceptions.LogonFailure,
                smb_exceptions.WrongPassword,
                smb_exceptions.PasswordExpired,
            ),
        ):
            return ErrorType.AUTH
        if isinstance(
            exc,
            (smb_exceptions.AccessDenied, smb_exceptions.PrivilegeNotHeld),
        ):
            return ErrorType.PERMISSION
        if isinstance(
            exc,
            (
                smb_exceptions.BadNetworkName,
                smb_exceptions.NoSuchFile,
                smb_exceptions.NotFound,
                smb_exceptions.ObjectNameNotFound,
                smb_exceptions.ObjectPathNotFound,
            ),
        ):
            return ErrorType.TARGET_MISSING
        if isinstance(exc, smb_exceptions.IOTimeout):
            return ErrorType.TCP_TIMEOUT
        if isinstance(exc, smb_exceptions.SMBConnectionClosed):
            return ErrorType.PARTIAL_TRANSFER
        if isinstance(exc, smb_exceptions.ServerUnavailable):
            return ErrorType.TCP_CONNECT
        if isinstance(exc, smb_exceptions.SMBException):
            # smbprotocol raises the base class, rather than IOTimeout, when
            # Connection.receive exhausts its client-side response deadline.
            # Keep that bounded wait retryable like every other socket timeout.
            message = str(exc).casefold()
            if "connection timeout" in message or "timed out" in message:
                return ErrorType.TCP_TIMEOUT
            cause = exc.__cause__
            if isinstance(cause, smb_exceptions.SMBException):
                cause_type = _classify_exception(cause, visited=visited)
                if cause_type != ErrorType.UNKNOWN:
                    return cause_type
            return ErrorType.PROTOCOL
    except ImportError:
        pass

    try:
        import paramiko

        if isinstance(exc, paramiko.BadHostKeyException):
            return ErrorType.TLS
        if isinstance(
            exc,
            (paramiko.AuthenticationException, paramiko.PasswordRequiredException),
        ):
            return ErrorType.AUTH
        if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
            errors = tuple(getattr(exc, "errors", {}).values())
            if any(isinstance(error, socket.gaierror) for error in errors):
                return ErrorType.DNS
            if errors and all(
                isinstance(error, (TimeoutError, socket.timeout))
                for error in errors
            ):
                return ErrorType.TCP_TIMEOUT
            return ErrorType.TCP_CONNECT
        if isinstance(exc, paramiko.SSHException):
            # SSH sessions can raise the generic SSHException when the peer
            # closes a channel or transport during an otherwise valid
            # operation. Host-key and authentication failures were handled
            # above and must never be retried as transient failures.
            return ErrorType.PARTIAL_TRANSFER
    except ImportError:
        pass

    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ErrorType.TCP_TIMEOUT
        if isinstance(exc, httpx.ConnectError):
            if _exception_chain_contains(exc, ssl.SSLError):
                return ErrorType.TLS
            cause = exc.__cause__
            if isinstance(cause, socket.gaierror):
                return ErrorType.DNS
            return ErrorType.TCP_CONNECT
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return ErrorType.AUTH
            if status == 403:
                return ErrorType.PERMISSION
            if status == 404:
                return ErrorType.TARGET_MISSING
            if status in {408, 425, 429, 500, 502, 503, 504}:
                return ErrorType.PARTIAL_TRANSFER
            return ErrorType.PROTOCOL
        if isinstance(
            exc,
            (
                httpx.ReadError,
                httpx.WriteError,
                httpx.CloseError,
                httpx.RemoteProtocolError,
            ),
        ):
            return ErrorType.PARTIAL_TRANSFER
        if isinstance(exc, httpx.HTTPError):
            return ErrorType.PROTOCOL
    except ImportError:
        pass
    return None


def _exception_chain_contains(
    exc: BaseException,
    expected: type[BaseException] | tuple[type[BaseException], ...],
) -> bool:
    """Inspect wrapped transport causes without looping on malformed chains."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, expected):
            return True
        visited.add(id(current))
        current = _visible_wrapped_exception(current)
    return False


def _visible_wrapped_exception(exc: BaseException) -> BaseException | None:
    """Return the exception Python exposes as the relevant wrapped failure.

    An explicit cause has precedence over the implicit context. ``from None``
    deliberately suppresses that context, so it must not affect either the
    public taxonomy or retry decisions.
    """
    cause = exc.__cause__
    if cause is not None:
        return cause
    if exc.__suppress_context__:
        return None
    return exc.__context__
