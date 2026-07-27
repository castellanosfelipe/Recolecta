import sys

import win32api
import win32ts

from app.platform.detect import RuntimeMode, runtime_mode
from app.platform.tray_windows import create_tray


def test_dev_and_service_modes_do_not_create_desktop_tray() -> None:
    arguments = {
        "dashboard_url": "http://127.0.0.1:8091",
        "run_all": lambda: None,
        "shutdown": lambda: None,
        "status_provider": lambda: "ok",
    }
    assert runtime_mode("dev") == RuntimeMode.DEV
    assert runtime_mode("service") == RuntimeMode.HEADLESS
    assert create_tray(configured_mode="dev", **arguments) is None
    assert create_tray(configured_mode="service", **arguments) is None


def test_runtime_detection_handles_platform_system_and_sessions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert runtime_mode("windows") == RuntimeMode.DEV

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "SYSTEM")
    assert runtime_mode("windows") == RuntimeMode.HEADLESS

    monkeypatch.setenv("USERNAME", "operator")
    monkeypatch.setattr(win32api, "GetCurrentProcessId", lambda: 42)
    monkeypatch.setattr(win32ts, "ProcessIdToSessionId", lambda process_id: 0)
    assert runtime_mode("windows") == RuntimeMode.HEADLESS

    monkeypatch.setattr(win32ts, "ProcessIdToSessionId", lambda process_id: 3)
    assert runtime_mode("windows") == RuntimeMode.INTERACTIVE

    def unavailable(process_id):
        raise OSError("session unavailable")

    monkeypatch.setattr(win32ts, "ProcessIdToSessionId", unavailable)
    assert runtime_mode("windows") == RuntimeMode.INTERACTIVE
