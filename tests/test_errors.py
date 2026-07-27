import errno
import socket
import ssl

import pytest

from app.errors import ErrorType, HarvesterError, classify_exception, is_retryable


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (socket.gaierror(), ErrorType.DNS),
        (TimeoutError(), ErrorType.TCP_TIMEOUT),
        (ssl.SSLError(), ErrorType.TLS),
        (PermissionError(), ErrorType.PERMISSION),
        (FileNotFoundError(), ErrorType.TARGET_MISSING),
        (ConnectionRefusedError(), ErrorType.TCP_CONNECT),
        (OSError(errno.ENOSPC, "full"), ErrorType.DISK_SPACE),
        (ValueError(), ErrorType.UNKNOWN),
    ],
)
def test_classify_exception(exc: BaseException, expected: ErrorType) -> None:
    assert classify_exception(exc) == expected


def test_harvester_error_keeps_category() -> None:
    exc = HarvesterError(ErrorType.INTEGRITY, "El tamaño no coincide.")
    assert classify_exception(exc) == ErrorType.INTEGRITY


def test_auth_is_not_retryable() -> None:
    assert not is_retryable(ErrorType.AUTH)
    assert is_retryable(ErrorType.TCP_TIMEOUT)
