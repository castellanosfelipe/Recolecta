from __future__ import annotations

import json
from pathlib import Path

import pytest

import installer


def _payload(root: Path) -> Path:
    payload = root / "payload"
    payload.mkdir()
    (payload / "Recolecta.exe").write_bytes(b"frozen executable")
    (payload / "install.ps1").write_text("", encoding="utf-8")
    (payload / "install-service.ps1").write_text("", encoding="utf-8")
    (payload / "uninstall.ps1").write_text("", encoding="utf-8")
    return payload


def test_default_install_directories_follow_windows_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\operator\\AppData\\Local")
    monkeypatch.setenv("PROGRAMDATA", "C:\\ProgramData")
    assert installer._default_install_dir(False) == Path(
        "C:\\Users\\operator\\AppData\\Local\\Recolecta"
    )
    assert installer._default_install_dir(True) == Path(
        "C:\\ProgramData\\Recolecta"
    )


def test_extract_only_copies_payload_without_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(tmp_path)
    destination = tmp_path / "installed"
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "_payload_dir", lambda: payload)
    monkeypatch.setattr(
        installer,
        "_powershell",
        lambda *args: pytest.fail("PowerShell must not run"),
    )

    result = installer.install(
        destination,
        service=False,
        port=8091,
        extract_only=True,
    )

    assert result == destination.resolve()
    assert (destination / "Recolecta.exe").read_bytes() == b"frozen executable"
    report = json.loads(
        (destination / "install-report.json").read_text(encoding="utf-8")
    )
    assert report["result"] == "extracted"
    assert report["extract_only"] is True


def test_service_install_requires_administrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "_is_admin", lambda: False)
    with pytest.raises(PermissionError, match="administrador"):
        installer.install(
            tmp_path / "service",
            service=True,
            port=8091,
            extract_only=False,
        )


def test_user_install_preserves_state_and_registers_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload(tmp_path)
    destination = tmp_path / "installed"
    data = destination / "data"
    data.mkdir(parents=True)
    (data / "recolecta.db").write_bytes(b"user state")
    (destination / "uninstall.ps1").write_text("", encoding="utf-8")
    calls: list[tuple[Path, tuple[str, ...]]] = []
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "_payload_dir", lambda: payload)
    monkeypatch.setattr(
        installer,
        "_powershell",
        lambda script, *args: calls.append((script, args)),
    )

    installer.install(
        destination,
        service=False,
        port=8123,
        extract_only=False,
    )

    assert (data / "recolecta.db").read_bytes() == b"user state"
    assert calls[0][0].name == "uninstall.ps1"
    assert calls[1] == (destination / "install.ps1", ("-Port", "8123"))
    report = json.loads(
        (destination / "install-report.json").read_text(encoding="utf-8")
    )
    assert report["result"] == "installed"
