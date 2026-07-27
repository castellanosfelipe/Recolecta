"""Lazy Windows Event Log integration for headless service mode."""

from __future__ import annotations


def write_event(message: str, *, error: bool = True) -> None:
    try:
        import win32evtlog
        import win32evtlogutil
    except ImportError as exc:
        raise RuntimeError(
            "pywin32 no está disponible para escribir en Windows Event Log."
        ) from exc
    event_type = (
        win32evtlog.EVENTLOG_ERROR_TYPE
        if error
        else win32evtlog.EVENTLOG_INFORMATION_TYPE
    )
    win32evtlogutil.ReportEvent(
        "FileHarvester",
        1,
        eventType=event_type,
        strings=[message],
    )
