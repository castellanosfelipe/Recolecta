"""Offline self-extracting Windows installer for Recolecta."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Sequence


PRODUCT_NAME = "Recolecta"
DASHBOARD_URL = "http://127.0.0.1:8091"
CREATE_NO_WINDOW = 0x08000000


def _payload_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS)
    else:
        root = Path(__file__).resolve().parent
    payload = root / "payload" / PRODUCT_NAME
    if not payload.is_dir():
        development_payload = root / "dist" / PRODUCT_NAME
        if development_payload.is_dir():
            return development_payload
        raise FileNotFoundError(
            "El instalador no contiene el bundle de Recolecta."
        )
    return payload


def _default_install_dir(service: bool) -> Path:
    variable = "PROGRAMDATA" if service else "LOCALAPPDATA"
    configured = os.environ.get(variable, "").strip()
    if configured:
        base = Path(configured)
    elif service:
        base = Path(os.environ.get("SystemDrive", "C:") + "\\ProgramData")
    else:
        base = Path.home() / "AppData" / "Local"
    return base / PRODUCT_NAME


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _powershell(script: Path, *arguments: str) -> None:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            detail or f"{script.name} terminó con código {completed.returncode}."
        )


def _copy_payload(payload: Path, destination: Path) -> None:
    payload = payload.resolve()
    destination = destination.expanduser().resolve()
    if destination == payload or payload in destination.parents:
        raise ValueError("La carpeta de instalación no puede contener el payload.")

    existing_uninstaller = destination / "uninstall.ps1"
    if existing_uninstaller.is_file():
        _powershell(existing_uninstaller)

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(payload, destination, dirs_exist_ok=True)
    executable = destination / "Recolecta.exe"
    if not executable.is_file():
        raise FileNotFoundError("El payload no produjo Recolecta.exe.")


def install(
    destination: Path,
    *,
    service: bool,
    port: int,
    extract_only: bool,
) -> Path:
    """Extract the application and optionally register its scheduled task."""
    if sys.platform != "win32":
        raise RuntimeError("Recolecta-Setup solo puede ejecutarse en Windows.")
    if service and not extract_only and not _is_admin():
        raise PermissionError(
            "La instalación como SYSTEM requiere ejecutar el Setup como administrador."
        )

    resolved = destination.expanduser().resolve()
    _copy_payload(_payload_dir(), resolved)
    report = {
        "app": PRODUCT_NAME,
        "install_dir": str(resolved),
        "mode": "service" if service else "user",
        "extract_only": extract_only,
        "result": "extracted" if extract_only else "installed",
    }
    if not extract_only:
        script = resolved / (
            "install-service.ps1" if service else "install.ps1"
        )
        _powershell(script, "-Port", str(port))
    (resolved / "install-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return resolved


def _message(text: str, *, error: bool = False, confirm: bool = False) -> int:
    if sys.platform != "win32":
        return 1
    style = 0x10 if error else 0x40
    if confirm:
        style |= 0x1
    return int(
        ctypes.windll.user32.MessageBoxW(
            None,
            text,
            f"{PRODUCT_NAME} Setup",
            style,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Recolecta-Setup",
        description="Instalador offline de Recolecta para Windows.",
    )
    parser.add_argument("--install-dir", type=Path)
    parser.add_argument(
        "--service",
        action="store_true",
        help="Instalar como SYSTEM al iniciar Windows (requiere administrador).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8091,
        help="Puerto local del dashboard (predeterminado: 8091).",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Extraer el bundle sin registrar tareas ni iniciar la aplicación.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    supplied = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(supplied)
    if not 1 <= args.port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")

    interactive = not supplied
    destination = args.install_dir or _default_install_dir(args.service)
    if interactive:
        response = _message(
            "Recolecta se instalará para el usuario actual en:\n\n"
            f"{destination}\n\n¿Desea continuar?",
            confirm=True,
        )
        if response != 1:
            return 0

    try:
        installed = install(
            destination,
            service=args.service,
            port=args.port,
            extract_only=args.extract_only,
        )
    except Exception as exc:
        if interactive:
            _message(f"No se pudo instalar Recolecta:\n\n{exc}", error=True)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if interactive:
        _message(
            f"Recolecta quedó instalada en:\n\n{installed}\n\n"
            f"Dashboard: {DASHBOARD_URL}"
        )
        webbrowser.open(DASHBOARD_URL)
    else:
        print(f"Recolecta quedó disponible en {installed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
