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
    if isinstance(exc, RecolectaError):
        return exc.error_type
    optional = _classify_optional_dependency_exception(exc)
    if optional is not None:
        return optional
    if isinstance(exc, ftplib.error_perm):
        response = str(exc).lstrip()
        if response.startswith("530"):
            return ErrorType.AUTH
        if response.startswith("550"):
            return ErrorType.PERMISSION
        return ErrorType.PROTOCOL
    if isinstance(exc, (ftplib.error_temp, ftplib.error_reply, ftplib.error_proto)):
        return ErrorType.PROTOCOL
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
        if exc.errno in {errno.EIO, getattr(errno, "EROFS", -1)}:
            return ErrorType.DISK_WRITE
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
) -> ErrorType | None:
    try:
        import paramiko

        if isinstance(exc, paramiko.BadHostKeyException):
            return ErrorType.TLS
        if isinstance(
            exc,
            (paramiko.AuthenticationException, paramiko.PasswordRequiredException),
        ):
            return ErrorType.AUTH
        if isinstance(exc, paramiko.SSHException):
            return ErrorType.PROTOCOL
    except ImportError:
        pass

    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ErrorType.TCP_TIMEOUT
        if isinstance(exc, httpx.ConnectError):
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
            return ErrorType.PROTOCOL
        if isinstance(exc, httpx.HTTPError):
            return ErrorType.PROTOCOL
    except ImportError:
        pass
    return None
