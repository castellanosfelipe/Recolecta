import errno
import ftplib
import socket
import ssl

import httpx
import paramiko
import pytest
from smbprotocol import exceptions as smb_exceptions
from smbprotocol.header import NtStatus, SMB2HeaderResponse

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
        (ftplib.error_temp("426 Transfer aborted"), ErrorType.PARTIAL_TRANSFER),
        (ftplib.Error("Malformed FTP response"), ErrorType.PROTOCOL),
        (EOFError("FTP control channel closed"), ErrorType.PARTIAL_TRANSFER),
        (BrokenPipeError("FTP data channel closed"), ErrorType.PARTIAL_TRANSFER),
        (OSError(errno.EPIPE, "broken pipe"), ErrorType.PARTIAL_TRANSFER),
        (WindowsNetworkError(86), ErrorType.AUTH),
        (WindowsNetworkError(1326), ErrorType.AUTH),
        (WindowsNetworkError(5), ErrorType.PERMISSION),
        (WindowsNetworkError(53), ErrorType.TARGET_MISSING),
        (paramiko.AuthenticationException("bad"), ErrorType.AUTH),
        (paramiko.SSHException("channel closed"), ErrorType.PARTIAL_TRANSFER),
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
    ("response", "expected"),
    (
        (
            "550 The system cannot find the file specified.",
            ErrorType.TARGET_MISSING,
        ),
        (
            "550 /entrada: No such file or directory",
            ErrorType.TARGET_MISSING,
        ),
        ("550 Requested directory does not exist", ErrorType.TARGET_MISSING),
        ("550 Access is denied.", ErrorType.PERMISSION),
        ("550 Permission denied.", ErrorType.PERMISSION),
        # A bare RFC 959 response cannot distinguish absence from access
        # control, so it deliberately preserves the conservative default.
        ("550 Requested action not taken.", ErrorType.PERMISSION),
    ),
)
def test_ftp_550_distinguishes_known_missing_and_permission_messages(
    response: str,
    expected: ErrorType,
) -> None:
    assert classify_exception(ftplib.error_perm(response)) == expected


@pytest.mark.parametrize(
    ("response", "expected"),
    (
        (
            "421 Service not available, closing control connection",
            ErrorType.TCP_CONNECT,
        ),
        ("425 Can't open data connection", ErrorType.TCP_CONNECT),
        ("426 Connection closed; transfer aborted", ErrorType.PARTIAL_TRANSFER),
        ("450 Requested file action not taken", ErrorType.PARTIAL_TRANSFER),
    ),
)
def test_ftp_temporary_replies_distinguish_connection_and_partial_transfer(
    response: str,
    expected: ErrorType,
) -> None:
    error_type = classify_exception(ftplib.error_temp(response))

    assert error_type == expected
    assert is_retryable(error_type)


def test_paramiko_no_valid_connections_is_a_retryable_connect_failure() -> None:
    exc = paramiko.ssh_exception.NoValidConnectionsError(
        {("127.0.0.1", 22): ConnectionRefusedError("refused")}
    )
    error_type = classify_exception(exc)
    assert error_type == ErrorType.TCP_CONNECT
    assert is_retryable(error_type)


def test_http_read_error_is_a_retryable_partial_transfer() -> None:
    request = httpx.Request("GET", "https://example.test/file")
    error_type = classify_exception(httpx.ReadError("closed", request=request))
    assert error_type == ErrorType.PARTIAL_TRANSFER
    assert is_retryable(error_type)


def test_http_connect_certificate_failure_is_not_retryable() -> None:
    request = httpx.Request("GET", "https://example.test/file")
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("TLS failed", request=request) from cause
    except httpx.ConnectError as exc:
        error_type = classify_exception(exc)
    assert error_type == ErrorType.TLS
    assert not is_retryable(error_type)


@pytest.mark.parametrize(
    ("exc", "expected", "retryable"),
    (
        (smb_exceptions.SMBAuthenticationError("bad"), ErrorType.AUTH, False),
        (smb_exceptions.LogonFailure(), ErrorType.AUTH, False),
        (smb_exceptions.WrongPassword(), ErrorType.AUTH, False),
        (smb_exceptions.PasswordExpired(), ErrorType.AUTH, False),
        (smb_exceptions.AccessDenied(), ErrorType.PERMISSION, False),
        (smb_exceptions.PrivilegeNotHeld(), ErrorType.PERMISSION, False),
        (smb_exceptions.BadNetworkName(), ErrorType.TARGET_MISSING, False),
        (smb_exceptions.ObjectNameNotFound(), ErrorType.TARGET_MISSING, False),
        (smb_exceptions.ObjectPathNotFound(), ErrorType.TARGET_MISSING, False),
        (smb_exceptions.IOTimeout(), ErrorType.TCP_TIMEOUT, True),
        (
            smb_exceptions.SMBException(
                "Connection timeout of 7.5 seconds exceeded"
            ),
            ErrorType.TCP_TIMEOUT,
            True,
        ),
        (
            smb_exceptions.SMBConnectionClosed("closed"),
            ErrorType.PARTIAL_TRANSFER,
            True,
        ),
        (smb_exceptions.ServerUnavailable(), ErrorType.TCP_CONNECT, True),
        (smb_exceptions.SMBException("invalid"), ErrorType.PROTOCOL, False),
    ),
)
def test_smb_exception_classification(
    exc: BaseException,
    expected: ErrorType,
    retryable: bool,
) -> None:
    error_type = classify_exception(exc)
    assert error_type == expected
    assert is_retryable(error_type) is retryable


def test_wrapped_socket_failure_preserves_network_taxonomy() -> None:
    try:
        try:
            raise socket.gaierror("name not known")
        except socket.gaierror as cause:
            raise ValueError("SMB failed to connect") from cause
    except ValueError as exc:
        assert classify_exception(exc) == ErrorType.DNS


def test_implicit_exception_context_preserves_network_taxonomy() -> None:
    try:
        try:
            raise socket.timeout("timed out")
        except socket.timeout:
            raise RuntimeError("listing failed")
    except RuntimeError as exc:
        assert exc.__cause__ is None
        assert classify_exception(exc) == ErrorType.TCP_TIMEOUT


def test_suppressed_exception_context_does_not_change_taxonomy() -> None:
    try:
        try:
            raise PermissionError("contexto que se ocultó deliberadamente")
        except PermissionError:
            raise ValueError("fallo público") from None
    except ValueError as exc:
        assert exc.__suppress_context__ is True
        assert classify_exception(exc) == ErrorType.UNKNOWN


def test_explicit_unknown_cause_takes_precedence_over_implicit_context() -> None:
    explicit_cause = ValueError("causa explícita")
    try:
        try:
            raise PermissionError("contexto implícito")
        except PermissionError:
            raise RuntimeError("fallo exterior") from explicit_cause
    except RuntimeError as exc:
        assert exc.__context__ is not None
        assert exc.__cause__ is explicit_cause
        assert classify_exception(exc) == ErrorType.UNKNOWN


def test_http_connect_ignores_suppressed_tls_context() -> None:
    request = httpx.Request("GET", "https://example.test/file")
    try:
        try:
            raise ssl.SSLCertVerificationError("certificado anterior")
        except ssl.SSLError:
            raise httpx.ConnectError("conexión rechazada", request=request) from None
    except httpx.ConnectError as exc:
        assert classify_exception(exc) == ErrorType.TCP_CONNECT


def test_exception_chain_cycle_is_safe_and_keeps_generic_errors_unknown() -> None:
    outer = RuntimeError("outer")
    inner = ValueError("inner")
    outer.__cause__ = inner
    inner.__context__ = outer

    assert classify_exception(outer) == ErrorType.UNKNOWN


@pytest.mark.parametrize(
    "exc",
    (
        EOFError("closed"),
        BrokenPipeError("closed"),
        OSError(errno.EPIPE, "closed"),
    ),
)
def test_closed_stream_failures_are_retryable(exc: BaseException) -> None:
    error_type = classify_exception(exc)

    assert error_type == ErrorType.PARTIAL_TRANSFER
    assert is_retryable(error_type)


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (NtStatus.STATUS_LOGON_FAILURE, ErrorType.AUTH),
        (NtStatus.STATUS_ACCESS_DENIED, ErrorType.PERMISSION),
        (NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, ErrorType.TARGET_MISSING),
        (NtStatus.STATUS_IO_TIMEOUT, ErrorType.TCP_TIMEOUT),
        (NtStatus.STATUS_SERVER_UNAVAILABLE, ErrorType.TCP_CONNECT),
    ),
)
def test_smb_response_status_uses_specific_subclass(
    status: int,
    expected: ErrorType,
) -> None:
    header = SMB2HeaderResponse()
    header["status"] = status
    assert classify_exception(smb_exceptions.SMBResponseException(header)) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorType.AUTH),
        (403, ErrorType.PERMISSION),
        (404, ErrorType.TARGET_MISSING),
        (500, ErrorType.PARTIAL_TRANSFER),
    ],
)
def test_http_status_classification(status: int, expected: ErrorType) -> None:
    request = httpx.Request("GET", "https://example.test/file")
    response = httpx.Response(status, request=request)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    assert classify_exception(raised.value) == expected
