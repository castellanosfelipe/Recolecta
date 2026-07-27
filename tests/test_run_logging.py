import json
from datetime import datetime, timezone
from pathlib import Path

from app.run_logging import RunLogStore


def test_jsonl_is_append_only_safe_and_redacted(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path)
    log = store.create(
        run_id=12,
        connection_name="SFTP Producción / Norte",
        started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    log.write(
        "run_started",
        password="no-debe-salir",
        url="sftp://usuario:clave@example.test/in",
    )
    log.write("run_finished", status="ok")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    values = [json.loads(line) for line in lines]
    assert log.path.name == "2026-07-27_sftp-produccion-norte_12.jsonl"
    assert [value["event"] for value in values] == [
        "run_started",
        "run_finished",
    ]
    assert values[0]["password"] == "***"
    assert "clave" not in lines[0]
    assert store.find(12) == log.path
