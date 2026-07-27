from pathlib import Path

import pytest

from launcher import main


def test_self_test_passes(capsys) -> None:
    assert main(["--self-test"]) == 0
    assert "autodiagnóstico correcto" in capsys.readouterr().out


def test_run_now_cli_parses_date_and_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured = {}
    monkeypatch.setenv("RECOLECTA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECOLECTA_MODE", "dev")

    def execute(config, **arguments):
        captured.update(arguments)
        return 0

    monkeypatch.setattr("app.commands.execute_run_now", execute)
    assert (
        main(
            [
                "--run-now",
                "--connection",
                "4",
                "--date",
                "2026-07-26",
                "--dry-run",
            ]
        )
        == 0
    )
    assert captured["connection_id"] == 4
    assert captured["selected_date"].isoformat() == "2026-07-26"
    assert captured["dry_run"] is True


def test_cli_rejects_invalid_date() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--run-now", "--date", "26-07-2026"])
    assert raised.value.code == 2
