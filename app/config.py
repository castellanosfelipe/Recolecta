"""Application configuration and portable path resolution."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "FileHarvester"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8091


def base_dir() -> Path:
    """Return the portable application data root."""
    env = os.environ.get("HARVESTER_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} debe ser 0/1, true/false, yes/no u on/off.")


def _env_port(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un número entero.") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} debe estar entre 1 y 65535.")
    return port


@dataclass(frozen=True)
class AppPaths:
    """Resolved paths for all portable application state."""

    root: Path
    data: Path
    logs: Path
    run_logs: Path
    exports: Path
    downloads: Path

    @classmethod
    def from_root(cls, root: Path | None = None) -> "AppPaths":
        resolved_root = (root or base_dir()).expanduser().resolve()
        logs = resolved_root / "logs"
        return cls(
            root=resolved_root,
            data=resolved_root / "data",
            logs=logs,
            run_logs=logs / "runs",
            exports=resolved_root / "exports",
            downloads=resolved_root / "downloads",
        )

    def ensure(self) -> "AppPaths":
        """Create runtime directories and return this immutable path set."""
        for directory in (
            self.data,
            self.logs,
            self.run_logs,
            self.exports,
            self.downloads,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class AppConfig:
    """Environment-derived process settings."""

    host: str
    port: int
    bind_lan: bool
    dashboard_user: str | None
    dashboard_password: str | None
    mode: str
    paths: AppPaths

    @classmethod
    def from_env(cls, *, create_directories: bool = True) -> "AppConfig":
        bind_lan = _env_bool("HARVESTER_BIND_LAN")
        user = os.environ.get("HARVESTER_DASH_USER", "").strip() or None
        password = os.environ.get("HARVESTER_DASH_PASS", "").strip() or None
        if bool(user) != bool(password):
            raise ValueError(
                "HARVESTER_DASH_USER y HARVESTER_DASH_PASS deben definirse juntos."
            )

        mode = os.environ.get("HARVESTER_MODE", "").strip().lower() or (
            "windows" if sys.platform == "win32" else "dev"
        )
        if mode not in {"windows", "service", "dev"}:
            raise ValueError("HARVESTER_MODE debe ser windows, service o dev.")

        paths = AppPaths.from_root()
        if create_directories:
            paths.ensure()

        return cls(
            host="0.0.0.0" if bind_lan else DEFAULT_HOST,
            port=_env_port("HARVESTER_PORT", DEFAULT_PORT),
            bind_lan=bind_lan,
            dashboard_user=user,
            dashboard_password=password,
            mode=mode,
            paths=paths,
        )
