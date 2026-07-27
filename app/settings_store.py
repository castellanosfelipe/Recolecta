"""Typed JSON-backed global settings stored in SQLite."""

from __future__ import annotations

import json
from typing import Any

from app.db import Database
from app.models import utc_now_iso


class SettingsStore:
    """Small key/value store with deterministic JSON serialization."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: Any = None) -> Any:
        with self.database.connect() as database:
            row = database.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["value_json"])

    def set(self, key: str, value: Any) -> None:
        if not key.strip():
            raise ValueError("La clave del ajuste no puede estar vacía.")
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as database:
            database.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key.strip(), serialized, utc_now_iso()),
            )

    def delete(self, key: str) -> bool:
        with self.database.connect() as database:
            cursor = database.execute("DELETE FROM settings WHERE key = ?", (key,))
            return cursor.rowcount > 0

    def all(self) -> dict[str, Any]:
        with self.database.connect() as database:
            rows = database.execute(
                "SELECT key, value_json FROM settings ORDER BY key"
            ).fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}
