"""Windows system-tray controller loaded only in interactive sessions."""

from __future__ import annotations

import threading
import webbrowser
import logging
from collections.abc import Callable

from app.platform.detect import RuntimeMode, runtime_mode


STATUS_COLORS = {
    "ok": "#2fca91",
    "partial": "#f2b94b",
    "running": "#f2b94b",
    "failed": "#ef6170",
    "paused": "#7f8da1",
}
logger = logging.getLogger(__name__)


class TrayController:
    def __init__(
        self,
        *,
        dashboard_url: str,
        run_all: Callable[[], object],
        shutdown: Callable[[], None],
        status_provider: Callable[[], str],
    ) -> None:
        self.dashboard_url = dashboard_url
        self.run_all = run_all
        self.shutdown = shutdown
        self.status_provider = status_provider
        self._icon = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="harvester-tray",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._icon is not None:
            self._icon.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        import pystray

        self._icon = pystray.Icon(
            "FileHarvester",
            _icon_image(self.status_provider()),
            "FileHarvester",
            menu=pystray.Menu(
                pystray.MenuItem("Abrir dashboard", self._open),
                pystray.MenuItem("Ejecutar todas", self._execute),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", self._exit),
            ),
        )
        threading.Thread(
            target=self._refresh,
            name="harvester-tray-status",
            daemon=True,
        ).start()
        self._icon.run()

    def _refresh(self) -> None:
        previous = ""
        while not self._stop.wait(5):
            status = self.status_provider()
            if status != previous and self._icon is not None:
                self._icon.icon = _icon_image(status)
                self._icon.title = (
                    f"FileHarvester · {_status_label(status)}"
                )
                previous = status

    def _open(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.dashboard_url)

    def _execute(self, _icon=None, _item=None) -> None:
        threading.Thread(
            target=self._run_all_safely,
            name="tray-run-all",
            daemon=True,
        ).start()

    def _exit(self, _icon=None, _item=None) -> None:
        self.shutdown()

    def _run_all_safely(self) -> None:
        try:
            self.run_all()
        except Exception:
            logger.exception("La ejecución iniciada desde la bandeja falló.")


def create_tray(
    *,
    configured_mode: str,
    dashboard_url: str,
    run_all: Callable[[], object],
    shutdown: Callable[[], None],
    status_provider: Callable[[], str],
) -> TrayController | None:
    if runtime_mode(configured_mode) != RuntimeMode.INTERACTIVE:
        return None
    return TrayController(
        dashboard_url=dashboard_url,
        run_all=run_all,
        shutdown=shutdown,
        status_provider=status_provider,
    )


def _icon_image(status: str):
    from PIL import Image, ImageDraw

    color = STATUS_COLORS.get(status, STATUS_COLORS["paused"])
    image = Image.new("RGBA", (64, 64), (9, 15, 27, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (8, 14, 56, 51),
        radius=7,
        fill=color,
    )
    draw.rectangle((13, 10, 31, 20), fill=color)
    draw.line((21, 31, 43, 31), fill="#071a13", width=4)
    draw.line((32, 22, 32, 41), fill="#071a13", width=4)
    return image


def _status_label(status: str) -> str:
    return {
        "ok": "correcto",
        "partial": "parcial",
        "running": "en curso",
        "failed": "fallo",
        "paused": "en pausa",
    }.get(status, "sin estado")
