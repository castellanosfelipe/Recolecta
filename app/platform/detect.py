"""Detect whether desktop-only Windows integrations are available."""

from __future__ import annotations

import os
import sys
from enum import StrEnum


class RuntimeMode(StrEnum):
    INTERACTIVE = "interactive"
    HEADLESS = "headless"
    DEV = "dev"


def runtime_mode(configured_mode: str) -> RuntimeMode:
    """Return a conservative runtime classification."""
    if configured_mode == "service":
        return RuntimeMode.HEADLESS
    if configured_mode == "dev":
        return RuntimeMode.DEV
    if sys.platform != "win32":
        return RuntimeMode.DEV
    if os.environ.get("USERNAME", "").strip().upper() == "SYSTEM":
        return RuntimeMode.HEADLESS
    try:
        import win32api
        import win32ts

        session_id = win32ts.ProcessIdToSessionId(
            win32api.GetCurrentProcessId()
        )
        if session_id == 0:
            return RuntimeMode.HEADLESS
    except (ImportError, OSError):
        pass
    return RuntimeMode.INTERACTIVE
