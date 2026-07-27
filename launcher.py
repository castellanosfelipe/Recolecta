"""Static import entry point used by development and PyInstaller."""

from __future__ import annotations

import argparse
import importlib
import multiprocessing
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from app import __version__
from app.config import AppConfig
from app.logging_setup import configure_logging


SELF_TEST_IMPORTS = (
    "app.config",
    "app.commands",
    "app.db",
    "app.downloader",
    "app.errors",
    "app.exports",
    "app.integrity",
    "app.logging_setup",
    "app.main",
    "app.models",
    "app.naming",
    "app.platform.secretstore",
    "app.platform.detect",
    "app.platform.tray_windows",
    "app.platform.secrets_fernet",
    "app.platform.single_instance",
    "app.scheduler",
    "app.alerts",
    "app.retention",
    "app.run_logging",
    "app.transports.ftp",
    "app.transports.sftp",
    "app.transports.webdav",
    "app.transports.smb",
    "app.throttle",
    "apscheduler",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    "fastapi",
    "httpx",
    "paramiko",
    "uvicorn",
) + (("win32crypt",) if sys.platform == "win32" else ()) + (
    (
        "PIL.Image",
        "PIL.ImageDraw",
        "pystray",
        "pystray._win32",
        "winotify",
        "winsound",
    )
    if sys.platform == "win32" and getattr(sys, "frozen", False)
    else ()
)


def run_self_test(report_path: str | None = None) -> int:
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
        message = "Autodiagnóstico fallido:\n- " + "\n- ".join(failures)
        exit_code = 1
    else:
        message = f"Recolecta {__version__}: autodiagnóstico correcto."
        exit_code = 0
    if report_path:
        Path(report_path).write_text(message + "\n", encoding="utf-8")
    stream = sys.stderr if exit_code else sys.stdout
    if stream is not None:
        print(message, file=stream)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Recolecta")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--self-test-report", metavar="PATH", help=argparse.SUPPRESS
    )
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--connection", type=int, metavar="ID")
    parser.add_argument("--date", dest="selected_date", metavar="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--service", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test(args.self_test_report)
    if args.self_test_report:
        build_parser().error("--self-test-report requiere --self-test")
    if args.port is not None and not 1 <= args.port <= 65535:
        build_parser().error("--port debe estar entre 1 y 65535")

    if args.service and (
        args.run_now
        or args.connection is not None
        or args.selected_date
        or args.dry_run
    ):
        build_parser().error("--service solo puede iniciar el proceso residente")
    if args.connection is not None and args.connection < 1:
        build_parser().error("--connection debe ser mayor que cero")
    if (args.connection is not None or args.selected_date or args.dry_run) and not args.run_now:
        build_parser().error(
            "--connection, --date y --dry-run requieren --run-now"
        )
    selected_date = None
    if args.selected_date:
        try:
            selected_date = date.fromisoformat(args.selected_date)
        except ValueError:
            build_parser().error("--date debe usar el formato YYYY-MM-DD")
    config = AppConfig.from_env()
    if args.service:
        config = replace(config, mode="service")
    if args.port is not None:
        config = replace(config, port=args.port)
    configure_logging(config.paths.logs)
    if args.run_now:
        from app.commands import execute_run_now

        return execute_run_now(
            config,
            connection_id=args.connection,
            selected_date=selected_date,
            dry_run=args.dry_run,
        )
    from app.main import run_resident

    return run_resident(config)


if __name__ == "__main__":
    raise SystemExit(main())
