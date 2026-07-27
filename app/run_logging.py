"""Append-only structured logs for individual download runs."""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import redact_secrets


class RunEventLog:
    """Write one durable JSON object per run event."""

    def __init__(
        self,
        path: Path,
        *,
        wall_clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.path = path
        self.wall_clock = wall_clock
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **details: Any) -> None:
        timestamp = self.wall_clock()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        payload = {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "event": event,
            **_redacted(details),
        }
        line = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()


class RunLogStore:
    """Create and locate safe run-log filenames."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        run_id: int,
        connection_name: str,
        started_at: datetime,
    ) -> RunEventLog:
        if started_at.tzinfo is None:
            raise ValueError("started_at debe incluir zona horaria.")
        filename = (
            f"{started_at.astimezone(timezone.utc).date().isoformat()}_"
            f"{_slug(connection_name)}_{run_id}.jsonl"
        )
        return RunEventLog(self.directory / filename)

    def find(self, run_id: int) -> Path | None:
        matches = sorted(self.directory.glob(f"*_{int(run_id)}.jsonl"))
        return matches[-1] if matches else None

    def list_since(self, cutoff: datetime) -> list[Path]:
        cutoff_utc = cutoff.astimezone(timezone.utc)
        return sorted(
            path
            for path in self.directory.glob("*.jsonl")
            if datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ) >= cutoff_utc
        )


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return slug[:60] or "conexion"


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "***"
                if str(key).lower()
                in {"password", "secret", "passphrase", "secret_encrypted"}
                else _redacted(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redacted(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value
