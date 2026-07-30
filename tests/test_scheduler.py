from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import Fernet

from app.db import ConnectionRepository, Database, RunRepository
from app.models import Connection, Protocol
from app.orchestrator import calculate_window
from app.platform.secrets_fernet import FernetSecretStore
from app.scheduler import (
    CatchUpPlanner,
    ClockJumpDetector,
    SchedulerService,
    SchedulerSettings,
)
from app.settings_store import SettingsStore


@pytest.fixture
def scheduler_data(tmp_path: Path):
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(
            name="Nocturna",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            dest_root="downloads",
            timezone="America/Bogota",
        )
    )
    return database, connections, RunRepository(database), saved


def test_scheduler_settings_load_and_validation(tmp_path: Path) -> None:
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    store = SettingsStore(database)
    store.set("schedule.hour", 3)
    store.set("schedule.minute", 15)
    store.set("schedule.jitter_minutes", 4)
    store.set("catchup.max_days", 5)
    settings = SchedulerSettings.load(store)
    assert settings.hour == 3
    assert settings.minute == 15
    assert settings.jitter_minutes == 4
    assert settings.catchup_max_days == 5
    with pytest.raises(ValueError, match="0 y 23"):
        SchedulerSettings(hour=24).validated()


def test_apscheduler_jobs_preserve_misfire_and_coalescing(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data

    class Coordinator:
        def execute_connection(self, *args, **kwargs):
            return None

    aps = BackgroundScheduler(timezone=timezone.utc)
    service = SchedulerService(
        Coordinator(),
        connections,
        runs,
        scheduler=aps,
    )
    service.configure(
        SchedulerSettings(hour=2, minute=7, jitter_minutes=3)
    )
    job = aps.get_job(f"connection-{saved.id}")
    assert job is not None
    assert job.misfire_grace_time is None
    assert job.coalesce is True
    assert job.max_instances == 1
    assert "hour='2'" in str(job.trigger)
    assert "minute='4'" in str(job.trigger)
    assert job.trigger.jitter == 360
    assert aps.get_job("clock-jump-detector") is not None


def test_scheduler_adds_retention_job(scheduler_data) -> None:
    _, connections, runs, _ = scheduler_data
    service = SchedulerService(
        type("Coordinator", (), {})(),
        connections,
        runs,
        scheduler=BackgroundScheduler(timezone=timezone.utc),
        retention_callback=lambda days: days,
    )
    service.configure(SchedulerSettings(retention_days=45))
    assert service.scheduler.get_job("audit-retention") is not None


def test_each_connection_can_use_a_distinct_daily_time(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data
    connections.update(saved.id, {"schedule_time": "05:25"})
    fallback = connections.create(
        Connection(
            name="Hora global",
            protocol=Protocol.FTP,
            host="ftp.example.test",
            dest_root="downloads",
            timezone="America/Bogota",
        )
    )
    service = SchedulerService(
        type("Coordinator", (), {})(),
        connections,
        runs,
        scheduler=BackgroundScheduler(timezone=timezone.utc),
    )

    service.configure(SchedulerSettings(hour=2, minute=7))

    custom_trigger = str(
        service.scheduler.get_job(f"connection-{saved.id}").trigger
    )
    fallback_trigger = str(
        service.scheduler.get_job(f"connection-{fallback.id}").trigger
    )
    assert "hour='5'" in custom_trigger
    assert "minute='25'" in custom_trigger
    assert "hour='2'" in fallback_trigger
    assert "minute='7'" in fallback_trigger


def test_catchup_uses_connection_specific_time(scheduler_data) -> None:
    _, connections, runs, saved = scheduler_data
    connections.update(saved.id, {"schedule_time": "04:30"})

    candidates = CatchUpPlanner(runs).candidates(
        connections.list(enabled_only=True),
        now=datetime(2026, 7, 27, 10, tzinfo=timezone.utc),
        settings=SchedulerSettings(
            hour=2,
            minute=0,
            catchup_max_days=1,
            startup_delay_s=0,
        ),
    )

    assert [candidate.scheduled_for_utc for candidate in candidates] == [
        datetime(2026, 7, 27, 9, 30, tzinfo=timezone.utc)
    ]


def test_catchup_finds_only_windows_without_success(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data
    settings = SchedulerSettings(
        hour=2,
        minute=0,
        catchup_max_days=3,
        startup_delay_s=0,
    )
    now = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
    successful_schedule = datetime(
        2026, 7, 26, 7, tzinfo=timezone.utc
    )
    window = calculate_window(saved, started_at=successful_schedule)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="schedule",
        window_start_utc=window.start_utc,
        window_end_utc=window.end_utc,
        started_at=successful_schedule,
    )
    runs.finish_run(run_id, status="ok")
    candidates = CatchUpPlanner(runs).candidates(
        connections.list(enabled_only=True),
        now=now,
        settings=settings,
    )
    assert [candidate.scheduled_for_utc for candidate in candidates] == [
        datetime(2026, 7, 25, 7, tzinfo=timezone.utc),
        datetime(2026, 7, 27, 7, tzinfo=timezone.utc),
    ]


def test_full_reconciliation_collapses_catchup_to_latest_scan(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data
    connections.update(
        saved.id,
        {"full_local_reconciliation": True},
    )

    candidates = CatchUpPlanner(runs).candidates(
        connections.list(enabled_only=True),
        now=datetime(2026, 7, 27, 8, tzinfo=timezone.utc),
        settings=SchedulerSettings(
            hour=2,
            minute=0,
            catchup_max_days=5,
            startup_delay_s=0,
        ),
    )

    assert [candidate.scheduled_for_utc for candidate in candidates] == [
        datetime(2026, 7, 27, 7, tzinfo=timezone.utc)
    ]


def test_scheduler_executes_catchup_oldest_first_and_applies_delay(
    scheduler_data,
) -> None:
    _, connections, runs, _ = scheduler_data
    calls = []

    class Coordinator:
        def execute_connection(self, connection_id, **kwargs):
            calls.append((connection_id, kwargs))
            return kwargs["window_reference_at"]

    sleeps = []
    service = SchedulerService(
        Coordinator(),
        connections,
        runs,
        now=lambda: datetime(2026, 7, 27, 8, tzinfo=timezone.utc),
        sleeper=sleeps.append,
    )
    service.settings = SchedulerSettings(
        hour=2,
        minute=0,
        catchup_max_days=2,
        startup_delay_s=5,
    )
    result = service.run_catchup()
    assert sleeps == [5]
    assert [value for value in result.executions] == [
        datetime(2026, 7, 26, 7, tzinfo=timezone.utc),
        datetime(2026, 7, 27, 7, tzinfo=timezone.utc),
    ]
    assert all(call[1]["trigger"] == "catchup" for call in calls)


def test_clock_jump_detector_distinguishes_normal_time_from_resume() -> None:
    start = datetime(2026, 7, 27, tzinfo=timezone.utc)
    walls = iter(
        [
            start,
            start + timedelta(seconds=10),
            start + timedelta(seconds=210),
        ]
    )
    monotonic_values = iter([0.0, 10.0, 20.0])
    detector = ClockJumpDetector(
        wall_clock=lambda: next(walls),
        monotonic=lambda: next(monotonic_values),
        threshold_s=120,
    )
    assert not detector.tick()
    assert detector.tick()


def test_clock_jump_detector_handles_monotonic_advancing_during_sleep() -> None:
    start = datetime(2026, 7, 27, tzinfo=timezone.utc)
    walls = iter([start, start + timedelta(hours=6)])
    monotonic_values = iter([0.0, 6 * 3600.0])
    detector = ClockJumpDetector(
        wall_clock=lambda: next(walls),
        monotonic=lambda: next(monotonic_values),
        threshold_s=120,
    )
    assert detector.tick()


def test_scheduled_job_uses_nominal_time_despite_symmetric_jitter(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data
    calls = []

    class Coordinator:
        def execute_connection(self, connection_id, **kwargs):
            calls.append(kwargs)

    service = SchedulerService(
        Coordinator(),
        connections,
        runs,
        now=lambda: datetime(2026, 7, 27, 6, 58, tzinfo=timezone.utc),
    )
    service.settings = SchedulerSettings(
        hour=2,
        minute=0,
        jitter_minutes=3,
    )
    service._run_scheduled(saved.id)
    assert calls[0]["window_reference_at"] == datetime(
        2026, 7, 27, 7, tzinfo=timezone.utc
    )


def test_scheduled_job_maps_pre_midnight_jitter_to_next_day(
    scheduler_data,
) -> None:
    _, connections, runs, saved = scheduler_data
    connections.update(saved.id, {"schedule_time": "00:05"})
    calls = []

    class Coordinator:
        def execute_connection(self, connection_id, **kwargs):
            calls.append(kwargs)

    service = SchedulerService(
        Coordinator(),
        connections,
        runs,
        now=lambda: datetime(2026, 7, 28, 4, 52, tzinfo=timezone.utc),
    )
    service.settings = SchedulerSettings(jitter_minutes=15)

    service._run_scheduled(saved.id)

    assert calls[0]["window_reference_at"] == datetime(
        2026, 7, 28, 5, 5, tzinfo=timezone.utc
    )
