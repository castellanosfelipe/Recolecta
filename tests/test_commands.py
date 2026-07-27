from pathlib import Path

from app.commands import execute_run_now
from app.config import AppConfig


def config(monkeypatch, tmp_path: Path) -> AppConfig:
    monkeypatch.setenv("RECOLECTA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECOLECTA_MODE", "dev")
    monkeypatch.delenv("RECOLECTA_SECRET_KEY", raising=False)
    return AppConfig.from_env()


def test_direct_cli_run_with_no_connections_is_successful(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    result = execute_run_now(
        config(monkeypatch, tmp_path),
        connection_id=None,
        selected_date=None,
        dry_run=False,
    )
    assert result == 0
    assert '"executions": []' in capsys.readouterr().out


def test_cli_delegates_when_resident_mutex_exists(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"executions": [{"status": "ok"}]}

    monkeypatch.setattr(
        "app.commands.SingleInstance.try_acquire",
        lambda self: False,
    )

    def post(url, *, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("app.commands.httpx.post", post)
    result = execute_run_now(
        config(monkeypatch, tmp_path),
        connection_id=9,
        selected_date=None,
        dry_run=True,
    )
    assert result == 0
    assert captured["url"].endswith("/api/commands/run-now")
    assert captured["payload"]["connection_id"] == 9
    assert captured["payload"]["dry_run"] is True
    assert captured["payload"]["trigger"] == "cli"
    assert '"status": "ok"' in capsys.readouterr().out
