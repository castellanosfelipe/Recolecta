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
