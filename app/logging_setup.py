"""Logging configuration with credential redaction."""

from __future__ import annotations

import logging
import re
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final


LOG_FORMAT: Final = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|secret|passphrase)\b(\s*[:=]\s*)([^\s,;]+)"
)
_URL_CREDENTIAL = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"(?P<user>[^/@:\s]+):(?P<secret>[^/@\s]+)@",
    re.IGNORECASE,
)
_TOKEN_SECRET = re.compile(
    r"(?i)\b(dpapi(?:-machine)?|fernet):[A-Za-z0-9_+/=-]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|key|api_key|signature|sig|secret)=)[^&#\s]+"
)


def redact_secrets(value: object) -> str:
    """Return a log-safe representation of arbitrary text."""
    text = str(value)
    text = _KEY_VALUE_SECRET.sub(r"\1\2***", text)
    text = _URL_CREDENTIAL.sub(r"\g<scheme>\g<user>:***@", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    return _TOKEN_SECRET.sub(r"\1:***", text)


class SensitiveDataFilter(logging.Filter):
    """Redact secrets from message text and interpolation arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        if record.exc_info:
            rendered += "\n" + "".join(
                traceback.format_exception(*record.exc_info)
            )
            record.exc_info = None
            record.exc_text = None
        record.msg = redact_secrets(rendered)
        record.args = ()
        return True


def configure_logging(log_dir: Path, *, level: int = logging.INFO) -> Path:
    """Configure root logging and return the application log path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"

    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SensitiveDataFilter())

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    return log_path
