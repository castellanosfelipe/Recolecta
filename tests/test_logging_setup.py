import logging
from pathlib import Path

from app.logging_setup import configure_logging, redact_secrets


def test_redacts_named_secrets_urls_and_encrypted_tokens() -> None:
    source = (
        "password=hunter2 secret:abc passphrase=qwerty "
        "sftp://alice:badpass@example.test/in "
        "dpapi-machine:AAAA fernet:BBBB"
    )
    redacted = redact_secrets(source)
    for secret in ("hunter2", "abc", "qwerty", "badpass", "AAAA", "BBBB"):
        assert secret not in redacted
    assert "alice:***@" in redacted


def test_configured_log_never_writes_secret(tmp_path: Path) -> None:
    log_path = configure_logging(tmp_path)
    logging.getLogger("test").warning("password=%s", "visible-secret")
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert "visible-secret" not in contents
    assert "password=***" in contents
