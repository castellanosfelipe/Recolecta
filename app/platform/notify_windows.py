"""Lazy Windows toast notification channel."""

from __future__ import annotations


def show_notification(title: str, message: str) -> None:
    try:
        from winotify import Notification
    except ImportError as exc:
        raise RuntimeError(
            "winotify no está disponible para mostrar notificaciones."
        ) from exc
    notification = Notification(
        app_id="FileHarvester",
        title=title,
        msg=message,
    )
    notification.show()
