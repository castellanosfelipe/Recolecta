from pathlib import Path

import pytest

from app.config import AppConfig, AppPaths, DEFAULT_PORT, base_dir


def test_base_dir_honors_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = tmp_path / "portable"
    monkeypatch.setenv("RECOLECTA_DATA_DIR", f"  {expected}  ")
    assert base_dir() == expected


def test_paths_ensure_creates_portable_tree(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path).ensure()
    assert paths.data.is_dir()
    assert paths.run_logs.is_dir()
    assert paths.exports.is_dir()
    assert paths.downloads.is_dir()


def test_config_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RECOLECTA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RECOLECTA_PORT", raising=False)
    monkeypatch.delenv("RECOLECTA_BIND_LAN", raising=False)
    monkeypatch.delenv("RECOLECTA_DASH_USER", raising=False)
    monkeypatch.delenv("RECOLECTA_DASH_PASS", raising=False)
    config = AppConfig.from_env(create_directories=False)
    assert config.host == "127.0.0.1"
    assert config.port == DEFAULT_PORT


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_invalid_ports_have_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    monkeypatch.setenv("RECOLECTA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECOLECTA_PORT", value)
    with pytest.raises(ValueError, match="RECOLECTA_PORT"):
        AppConfig.from_env(create_directories=False)


def test_dashboard_credentials_must_be_a_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECOLECTA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECOLECTA_DASH_USER", "operator")
    monkeypatch.delenv("RECOLECTA_DASH_PASS", raising=False)
    with pytest.raises(ValueError, match="deben definirse juntos"):
        AppConfig.from_env(create_directories=False)
