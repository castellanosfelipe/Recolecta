"""Static import entry point used by development and PyInstaller."""

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import sys
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from app import __version__
from app.config import AppConfig
from app.logging_setup import configure_logging


SELF_TEST_IMPORTS = (
    "app.config",
    "app.db",
    "app.downloader",
    "app.errors",
    "app.integrity",
    "app.logging_setup",
    "app.models",
    "app.naming",
    "app.platform.secretstore",
    "app.platform.secrets_fernet",
    "app.transports.ftp",
    "app.transports.sftp",
    "app.transports.webdav",
    "app.transports.smb",
    "app.throttle",
    "cryptography.hazmat.primitives",
    "httpx",
    "paramiko",
) + (("win32crypt",) if sys.platform == "win32" else ())


def run_self_test() -> int:
    """Import foundational modules and validate runtime configuration."""
    failures: list[str] = []
    for module_name in SELF_TEST_IMPORTS:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - failure path is diagnostic
            failures.append(f"{module_name}: {exc}")

    try:
        config = AppConfig.from_env(create_directories=False)
        if config.port <= 0:
            failures.append("configuración de puerto inválida")
        ZoneInfo("America/Bogota")
    except Exception as exc:  # pragma: no cover - failure path is diagnostic
        failures.append(f"configuración: {exc}")

    if failures:
        print("Autodiagnóstico fallido:\n- " + "\n- ".join(failures), file=sys.stderr)
        return 1
    print(f"FileHarvester {__version__}: autodiagnóstico correcto.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="FileHarvester")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()

    config = AppConfig.from_env()
    configure_logging(config.paths.logs)
    print(
        "El servidor web se incorporará en la Fase 5. "
        "Use --self-test para validar este andamiaje."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
