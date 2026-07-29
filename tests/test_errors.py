import errno
import ftplib
import socket
import ssl

import httpx
import paramiko
import pytest

from app.errors import ErrorType, RecolectaError, classify_exception, is_retryable


class WindowsNetworkError(OSError):
    def __init__(self, winerror: int) -> None:
        super().__init__(winerror, "Windows network error")
        self.winerror = winerror


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
        (ftplib.error_perm("530 Login incorrect"), ErrorType.AUTH),
        (ftplib.error_perm("550 Permission denied"), ErrorType.PERMISSION),
        (WindowsNetworkError(86), ErrorType.AUTH),
        (WindowsNetworkError(1326), ErrorType.AUTH),
        (WindowsNetworkError(5), ErrorType.PERMISSION),
        (WindowsNetworkError(53), ErrorType.TARGET_MISSING),
        (paramiko.AuthenticationException("bad"), ErrorType.AUTH),
        (
            httpx.ConnectTimeout(
                "late", request=httpx.Request("GET", "https://example.test")
            ),
            ErrorType.TCP_TIMEOUT,
        ),
        (ValueError(), ErrorType.UNKNOWN),
    ],
)
def test_classify_exception(exc: BaseException, expected: ErrorType) -> None:
    assert classify_exception(exc) == expected


def test_recolecta_error_keeps_category() -> None:
    exc = RecolectaError(ErrorType.INTEGRITY, "El tamaño no coincide.")
    assert classify_exception(exc) == ErrorType.INTEGRITY


def test_auth_is_not_retryable() -> None:
    assert not is_retryable(ErrorType.AUTH)
    assert is_retryable(ErrorType.TCP_TIMEOUT)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorType.AUTH),
        (403, ErrorType.PERMISSION),
        (404, ErrorType.TARGET_MISSING),
        (500, ErrorType.PROTOCOL),
    ],
)
def test_http_status_classification(status: int, expected: ErrorType) -> None:
    request = httpx.Request("GET", "https://example.test/file")
    response = httpx.Response(status, request=request)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    assert classify_exception(raised.value) == expected
