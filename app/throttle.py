"""Courtesy spacing, global concurrency, and token-bucket bandwidth limits."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable


class TokenBucket:
    """Thread-safe byte token bucket with injectable time for deterministic tests."""

    def __init__(
        self,
        rate_bytes_per_second: float,
        *,
        capacity_bytes: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_bytes_per_second <= 0:
            raise ValueError("La tasa del token bucket debe ser positiva.")
        self.rate = float(rate_bytes_per_second)
        self.capacity = float(capacity_bytes or rate_bytes_per_second)
        if self.capacity <= 0:
            raise ValueError("La capacidad del token bucket debe ser positiva.")
        self._tokens = self.capacity
        self._clock = clock
        self._sleeper = sleeper
        self._updated_at = clock()
        self._lock = threading.Lock()

    def consume(
        self,
        amount: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        if amount < 0:
            raise ValueError("No se pueden consumir bytes negativos.")
        remaining = float(amount)
        while remaining > 0:
            request = min(remaining, self.capacity)
            while True:
                with self._lock:
                    now = self._clock()
                    elapsed = max(0.0, now - self._updated_at)
                    self._tokens = min(
                        self.capacity,
                        self._tokens + elapsed * self.rate,
                    )
                    self._updated_at = now
                    if self._tokens >= request:
                        self._tokens -= request
                        wait = 0.0
                    else:
                        wait = (request - self._tokens) / self.rate
                if wait <= 0:
                    break
                if cancel_event is not None:
                    if cancel_event.wait(wait):
                        return False
                else:
                    self._sleeper(wait)
            remaining -= request
        return True


class ThrottleManager:
    """Coordinate connection launch courtesy and global worker concurrency."""

    def __init__(
        self,
        *,
        global_parallelism: int = 4,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if global_parallelism < 1:
            raise ValueError("La concurrencia global debe ser al menos uno.")
        self._global = threading.BoundedSemaphore(global_parallelism)
        self._clock = clock
        self._sleeper = sleeper
        self._state_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._last_started: dict[str, float] = {}
        self._buckets: dict[str, TokenBucket] = {}

    @contextmanager
    def transfer_slot(
        self,
        host: str,
        *,
        minimum_spacing_s: float = 0.0,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[bool]:
        """Acquire host launch lock, spacing, then global semaphore."""
        key = host.casefold()
        with self._state_lock:
            host_lock = self._host_locks.setdefault(key, threading.Lock())
        if not _acquire_interruptibly(host_lock, cancel_event):
            yield False
            return
        try:
            with self._state_lock:
                previous = self._last_started.get(key)
            if previous is not None and minimum_spacing_s > 0:
                elapsed = self._clock() - previous
                if elapsed < minimum_spacing_s:
                    wait = minimum_spacing_s - elapsed
                    if cancel_event is not None:
                        if cancel_event.wait(wait):
                            yield False
                            return
                    else:
                        self._sleeper(wait)
            with self._state_lock:
                self._last_started[key] = self._clock()
        finally:
            host_lock.release()
        if not _acquire_interruptibly(self._global, cancel_event):
            yield False
            return
        try:
            yield True
        finally:
            self._global.release()

    def bandwidth_bucket(
        self, key: str, limit_kbps: int | None
    ) -> TokenBucket | None:
        if limit_kbps is None:
            return None
        with self._state_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                rate = limit_kbps * 1024.0
                bucket = TokenBucket(
                    rate,
                    capacity_bytes=rate,
                    clock=self._clock,
                    sleeper=self._sleeper,
                )
                self._buckets[key] = bucket
            return bucket


def _acquire_interruptibly(
    lock: Any,
    cancel_event: threading.Event | None,
) -> bool:
    if cancel_event is None:
        lock.acquire()
        return True
    while not cancel_event.is_set():
        if lock.acquire(timeout=0.1):
            return True
    return False
