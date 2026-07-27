"""Central error taxonomy and exception classification."""

from __future__ import annotations

import errno
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


class HarvesterError(Exception):
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
    if isinstance(exc, HarvesterError):
        return exc.error_type
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
