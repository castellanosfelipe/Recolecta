from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_powershell_scripts_parse() -> None:
    scripts = (
        "build.ps1",
        "install.ps1",
        "install-service.ps1",
        "uninstall.ps1",
        "scripts/update_hashes.ps1",
        "scripts/capture_dashboard.ps1",
        "scripts/acceptance_smoke.ps1",
    )
    quoted = ",".join(f"'{ROOT / script}'" for script in scripts)
    command = (
        f"$files=@({quoted}); foreach($file in $files)"
        "{[void][scriptblock]::Create((Get-Content -LiteralPath $file -Raw))}"
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_build_is_offline_gated_and_complete() -> None:
    script = _read("build.ps1")
    lowered = script.lower()
    assert "--no-index" in lowered
    assert "pip_no_index" in lowered
    assert lowered.index("-m pytest") < lowered.index("-m pyinstaller")
    assert "--onedir" in lowered
    assert "--noconsole" in lowered
    assert "--noconfirm" in lowered
    assert "--clean" in lowered
    assert "--self-test" in lowered[lowered.index("-m pyinstaller") :]
    for expected in (
        "static;static",
        "templates;templates",
        "win32crypt",
        "winotify",
        "winsound",
        "pystray._win32",
        "pil.image",
        "pil.imagedraw",
        "cryptography.hazmat",
        '"apscheduler"',
        '"tzdata"',
        "compress-archive",
        "install-service.ps1",
        "uninstall.ps1",
    ):
        assert expected in lowered


def test_user_install_task_has_resilience_settings() -> None:
    script = _read("install.ps1").lower()
    for expected in (
        "atlogon",
        "restartcount 999",
        "multipleinstances ignorenew",
        "executiontimelimit",
        "startwhenavailable",
        "allowstartifonbatteries",
        "dontstopifgoingonbatteries",
        "workingdirectory",
        "start-scheduledtask",
        "/healthz",
    ):
        assert expected in script


def test_service_task_uses_system_and_machine_mode() -> None:
    script = _read("install-service.ps1").lower()
    for expected in (
        "atstartup",
        '"system"',
        "serviceaccount",
        "highest",
        "waketorun",
        "--service",
        "startwhenavailable",
        "workingdirectory",
    ):
        assert expected in script


def test_uninstaller_preserves_user_data() -> None:
    script = _read("uninstall.ps1").lower()
    assert "unregister-scheduledtask" in script
    assert "stop-process" in script
    assert "remove-item" not in script
    assert "archivos descargados" in script


def test_offline_inventory_and_hashes() -> None:
    wheelhouse = ROOT / "wheelhouse"
    wheels = {path.name.lower() for path in wheelhouse.glob("*.whl")}
    requirements = _read("requirements.txt") + "\n" + _read(
        "requirements-dev.txt"
    )
    for raw_line in requirements.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        package = re.split(r"[=;<>\[]", line, maxsplit=1)[0]
        normalized = re.sub(r"[-_.]+", "-", package).lower()
        assert any(
            re.sub(r"[-_.]+", "-", filename).startswith(normalized + "-")
            for filename in wheels
        ), f"No wheel for {package}"

    installer = ROOT / "vendor" / "python-3.12.10-amd64.exe"
    assert installer.stat().st_size > 20_000_000
    for directory in (wheelhouse, ROOT / "vendor"):
        lines = (directory / "SHA256SUMS.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        assert lines
        for line in lines:
            digest, filename = line.split(" *", 1)
            payload = directory / filename
            assert payload.is_file()
            assert hashlib.sha256(payload.read_bytes()).hexdigest() == digest


def test_ci_packages_and_releases_tags() -> None:
    workflow = _read(".github/workflows/build-windows.yml").lower()
    for expected in (
        '"v*.*.*"',
        ".\\build.ps1",
        "expand-archive",
        "actions/upload-artifact@v4",
        "softprops/action-gh-release@v2",
        "recolecta-win64.zip",
        "sha256sums.txt",
        "acceptance-smoke.json",
    ):
        assert expected in workflow


def test_acceptance_smoke_uses_only_frozen_bundle() -> None:
    script = _read("scripts/acceptance_smoke.ps1").lower()
    for expected in (
        "expand-archive",
        "recolecta.exe",
        "--self-test",
        "/healthz",
        "/static/app.js",
        "totalseconds -ge 5",
        "external_python_required = $false",
        "get-filehash",
        "stop-process",
    ):
        assert expected in script
    assert "-m pytest" not in script
    assert "python.exe -" not in script


def test_legacy_product_name_is_absent_from_source() -> None:
    legacy_product = "".join(
        chr(value)
        for value in (
            102, 105, 108, 101, 104, 97, 114, 118, 101, 115, 116, 101, 114
        )
    )
    excluded = {
        ".git",
        ".python-build",
        ".venv-build",
        "build",
        "dist",
        "wheelhouse",
        "work",
    }
    text_suffixes = {
        ".css",
        ".html",
        ".ini",
        ".js",
        ".md",
        ".ps1",
        ".py",
        ".txt",
        ".yml",
    }
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or excluded.intersection(path.parts)
            or path.suffix.lower() not in text_suffixes
        ):
            continue
        assert legacy_product not in path.read_text(encoding="utf-8").lower(), path
