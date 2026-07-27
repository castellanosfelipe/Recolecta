"""CLI direct execution and delegation to the resident local API."""

from __future__ import annotations

import json
import time
from datetime import date

import httpx

from app.config import AppConfig
from app.main import build_runtime
from app.platform.single_instance import SingleInstance


def execute_run_now(
    config: AppConfig,
    *,
    connection_id: int | None,
    selected_date: date | None,
    dry_run: bool,
) -> int:
    """Delegate to the resident process or execute directly under the mutex."""
    guard = SingleInstance(config.paths.data)
    if not guard.try_acquire():
        return _delegate(
            config,
            connection_id=connection_id,
            selected_date=selected_date,
            dry_run=dry_run,
        )
    try:
        runtime = build_runtime(config)
        if connection_id is not None:
            executions = (
                runtime.coordinator.execute_connection(
                    connection_id,
                    trigger="cli",
                    selected_date=selected_date,
                    dry_run_only=dry_run,
                ),
            )
        else:
            executions = runtime.coordinator.execute_all(
                trigger="cli",
                selected_date=selected_date,
                dry_run_only=dry_run,
            )
        print(
            json.dumps(
                {"executions": [item.summary() for item in executions]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        print(f"No fue posible ejecutar la corrida: {exc}")
        return 1
    finally:
        guard.release()


def _delegate(
    config: AppConfig,
    *,
    connection_id: int | None,
    selected_date: date | None,
    dry_run: bool,
) -> int:
    payload = {
        "connection_id": connection_id,
        "selected_date": selected_date.isoformat() if selected_date else None,
        "dry_run": dry_run,
        "trigger": "cli",
    }
    url = f"http://127.0.0.1:{config.port}/api/commands/run-now"
    last_error: Exception | None = None
    for _ in range(20):
        try:
            response = httpx.post(url, json=payload, timeout=30.0)
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
            return 0
        except httpx.ConnectError as exc:
            last_error = exc
            time.sleep(0.25)
        except httpx.HTTPError as exc:
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None:
                detail = response.text
            print(f"La instancia residente rechazó la corrida: {detail or exc}")
            return 1
    print(
        "FileHarvester está activo, pero su API local no respondió. "
        f"Último error: {last_error}"
    )
    return 1
