"""Daily scheduling, startup catch-up, and suspend/resume detection."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.db import ConnectionRepository, RunRepository
from app.models import Connection, WindowMode
from app.orchestrator import RunCoordinator, RunExecution, calculate_window
from app.settings_store import SettingsStore


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerSettings:
    hour: int = 2
    minute: int = 0
    jitter_minutes: int = 0
    catchup_enabled: bool = True
    catchup_max_days: int = 3
    startup_delay_s: int = 60

    @classmethod
    def load(cls, settings: SettingsStore) -> "SchedulerSettings":
        return cls(
            hour=int(settings.get("schedule.hour", 2)),
            minute=int(settings.get("schedule.minute", 0)),
            jitter_minutes=int(settings.get("schedule.jitter_minutes", 0)),
            catchup_enabled=bool(settings.get("catchup.enabled", True)),
            catchup_max_days=int(settings.get("catchup.max_days", 3)),
            startup_delay_s=int(settings.get("catchup.startup_delay_s", 60)),
        ).validated()

    def validated(self) -> "SchedulerSettings":
        if not 0 <= self.hour <= 23:
            raise ValueError("schedule.hour debe estar entre 0 y 23.")
        if not 0 <= self.minute <= 59:
            raise ValueError("schedule.minute debe estar entre 0 y 59.")
        if self.jitter_minutes < 0:
            raise ValueError("El jitter no puede ser negativo.")
        if self.catchup_max_days < 1:
            raise ValueError("catchup.max_days debe ser al menos uno.")
        if self.startup_delay_s < 0:
            raise ValueError("startup_delay_s no puede ser negativo.")
        return self


@dataclass(frozen=True)
class CatchUpCandidate:
    connection_id: int
    connection_name: str
    scheduled_for_utc: datetime


@dataclass(frozen=True)
class CatchUpFailure:
    candidate: CatchUpCandidate
    message: str


@dataclass(frozen=True)
class CatchUpResult:
    executions: tuple[RunExecution, ...]
    failures: tuple[CatchUpFailure, ...]


class CatchUpPlanner:
    """Find scheduled windows without a corresponding successful run."""

    def __init__(self, runs: RunRepository) -> None:
        self.runs = runs

    def candidates(
        self,
        connections: tuple[Connection, ...] | list[Connection],
        *,
        now: datetime,
        settings: SchedulerSettings,
    ) -> tuple[CatchUpCandidate, ...]:
        if now.tzinfo is None:
            raise ValueError("now debe incluir zona horaria.")
        now_utc = now.astimezone(timezone.utc)
        candidates: list[CatchUpCandidate] = []
        for connection in connections:
            if not connection.enabled or connection.id is None:
                continue
            zone = ZoneInfo(connection.timezone)
            today = now_utc.astimezone(zone).date()
            last_end = self.runs.last_successful_end(connection.id)
            for days_ago in range(settings.catchup_max_days):
                scheduled_date = today - timedelta(days=days_ago)
                scheduled_local = datetime.combine(
                    scheduled_date,
                    wall_time(settings.hour, settings.minute),
                    tzinfo=zone,
                )
                scheduled_utc = scheduled_local.astimezone(timezone.utc)
                if scheduled_utc > now_utc:
                    continue
                if (
                    connection.window_mode == WindowMode.SINCE_LAST_RUN
                    and last_end is not None
                    and last_end >= scheduled_utc
                ):
                    continue
                window = calculate_window(
                    connection,
                    started_at=scheduled_utc,
                    last_successful_end_utc=last_end,
                )
                if self.runs.has_successful_window(
                    connection.id,
                    window_start_utc=window.start_utc,
                    window_end_utc=window.end_utc,
                ):
                    continue
                candidates.append(
                    CatchUpCandidate(
                        connection.id,
                        connection.name,
                        scheduled_utc,
                    )
                )
        return tuple(
            sorted(candidates, key=lambda candidate: candidate.scheduled_for_utc)
        )


class ClockJumpDetector:
    """Detect suspend/resume as wall-clock progress beyond monotonic progress."""

    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        threshold_s: float = 120.0,
    ) -> None:
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.threshold_s = threshold_s
        self._last_wall = wall_clock()
        self._last_monotonic = monotonic()

    def tick(self) -> bool:
        wall = self.wall_clock()
        monotonic_value = self.monotonic()
        wall_elapsed = (wall - self._last_wall).total_seconds()
        monotonic_elapsed = monotonic_value - self._last_monotonic
        self._last_wall = wall
        self._last_monotonic = monotonic_value
        return (
            wall_elapsed > self.threshold_s
            or monotonic_elapsed > self.threshold_s
            or abs(wall_elapsed - monotonic_elapsed) > self.threshold_s
        )


class SchedulerService:
    """Configure APScheduler jobs and execute catch-up safely."""

    def __init__(
        self,
        coordinator: RunCoordinator,
        connections: ConnectionRepository,
        runs: RunRepository,
        *,
        scheduler: BackgroundScheduler | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleeper: Callable[[float], None] = time.sleep,
        jump_detector: ClockJumpDetector | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.connections = connections
        self.runs = runs
        self.scheduler = scheduler or BackgroundScheduler(timezone=timezone.utc)
        self.now = now
        self.sleeper = sleeper
        self.jump_detector = jump_detector or ClockJumpDetector(wall_clock=now)
        self.settings = SchedulerSettings()

    def configure(self, settings: SchedulerSettings) -> None:
        self.settings = settings.validated()
        self.scheduler.remove_all_jobs()
        for connection in self.connections.list(enabled_only=True):
            if connection.id is None:
                continue
            base_hour, base_minute, jitter_seconds = _symmetric_jitter_fields(
                settings.hour,
                settings.minute,
                settings.jitter_minutes,
            )
            trigger = CronTrigger(
                hour=base_hour,
                minute=base_minute,
                timezone=ZoneInfo(connection.timezone),
                jitter=jitter_seconds or None,
            )
            self.scheduler.add_job(
                self._run_scheduled,
                trigger=trigger,
                args=(connection.id,),
                id=f"connection-{connection.id}",
                name=f"FileHarvester · {connection.name}",
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )
        self.scheduler.add_job(
            self._check_clock_jump,
            trigger=IntervalTrigger(seconds=60),
            id="clock-jump-detector",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    def run_catchup(
        self,
        *,
        apply_startup_delay: bool = True,
    ) -> CatchUpResult:
        if not self.settings.catchup_enabled:
            return CatchUpResult((), ())
        if apply_startup_delay and self.settings.startup_delay_s:
            self.sleeper(self.settings.startup_delay_s)
        planner = CatchUpPlanner(self.runs)
        candidates = planner.candidates(
            self.connections.list(enabled_only=True),
            now=self.now(),
            settings=self.settings,
        )
        executions: list[RunExecution] = []
        failures: list[CatchUpFailure] = []
        for candidate in candidates:
            try:
                executions.append(
                    self.coordinator.execute_connection(
                        candidate.connection_id,
                        trigger="catchup",
                        window_reference_at=candidate.scheduled_for_utc,
                    )
                )
            except Exception as exc:
                logger.exception(
                    "Catch-up falló para %s", candidate.connection_name
                )
                failures.append(CatchUpFailure(candidate, str(exc)))
        return CatchUpResult(tuple(executions), tuple(failures))

    def _run_scheduled(self, connection_id: int) -> None:
        try:
            connection = self.connections.get(connection_id)
            nominal = _nominal_scheduled_time(
                connection,
                now=self.now(),
                settings=self.settings,
            )
            self.coordinator.execute_connection(
                connection_id,
                trigger="schedule",
                window_reference_at=nominal,
            )
        except Exception:
            logger.exception(
                "La corrida programada falló para la conexión %s", connection_id
            )

    def _check_clock_jump(self) -> None:
        if self.jump_detector.tick():
            logger.info(
                "Se detectó reanudación del equipo; comprobando catch-up."
            )
            self.run_catchup(apply_startup_delay=False)


def _symmetric_jitter_fields(
    hour: int, minute: int, jitter_minutes: int
) -> tuple[int, int, int]:
    nominal_minutes = hour * 60 + minute
    base_minutes = (nominal_minutes - jitter_minutes) % (24 * 60)
    return (
        base_minutes // 60,
        base_minutes % 60,
        jitter_minutes * 2 * 60,
    )


def _nominal_scheduled_time(
    connection: Connection,
    *,
    now: datetime,
    settings: SchedulerSettings,
) -> datetime:
    now_utc = now.astimezone(timezone.utc)
    zone = ZoneInfo(connection.timezone)
    local_now = now_utc.astimezone(zone)
    nominal_today = datetime.combine(
        local_now.date(),
        wall_time(settings.hour, settings.minute),
        tzinfo=zone,
    )
    tolerance = timedelta(minutes=settings.jitter_minutes)
    if local_now < nominal_today and nominal_today - local_now > tolerance:
        nominal_today -= timedelta(days=1)
    return nominal_today.astimezone(timezone.utc)
