"""Cross-mode single-instance guard with a named Windows mutex."""

from __future__ import annotations

import sys
from pathlib import Path
from types import TracebackType


class SingleInstance:
    """Acquire once per machine on Windows and once per data dir in dev."""

    def __init__(
        self,
        data_dir: Path,
        *,
        mutex_name: str = r"Global\Recolecta.Singleton",
    ) -> None:
        self.data_dir = data_dir
        self.mutex_name = mutex_name
        self.acquired = False
        self._handle = None
        self._file = None

    def try_acquire(self) -> bool:
        if self.acquired:
            return True
        if sys.platform == "win32":
            import win32api
            import win32event
            import winerror

            handle = win32event.CreateMutex(None, True, self.mutex_name)
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                win32api.CloseHandle(handle)
                return False
            self._handle = handle
            self.acquired = True
            return True

        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / ".instance.lock"
        lock_file = lock_path.open("a+b")
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_file.close()
            return False
        self._file = lock_file
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        if sys.platform == "win32" and self._handle is not None:
            import win32api
            import win32event

            try:
                win32event.ReleaseMutex(self._handle)
            finally:
                win32api.CloseHandle(self._handle)
                self._handle = None
        elif self._file is not None:
            try:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None
        self.acquired = False

    def __enter__(self) -> "SingleInstance":
        if not self.try_acquire():
            raise RuntimeError("Recolecta ya se está ejecutando.")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
