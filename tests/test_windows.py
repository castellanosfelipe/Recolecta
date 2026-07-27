from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import Connection, Protocol, WindowMode
from app.orchestrator import calculate_window


def connection(**changes) -> Connection:
    base = Connection(
        name="Ventana",
        protocol=Protocol.SFTP,
        host="example.test",
        dest_root="downloads",
        timezone="America/Bogota",
    )
    return replace(base, **changes).normalized()


def test_calendar_day_uses_previous_local_day() -> None:
    result = calculate_window(
        connection(window_mode=WindowMode.CALENDAR_DAY),
        started_at=datetime(2026, 7, 27, 7, tzinfo=timezone.utc),
    )
    assert result.start_utc == datetime(2026, 7, 26, 5, tzinfo=timezone.utc)
    assert result.end_utc == datetime(2026, 7, 27, 5, tzinfo=timezone.utc)


def test_calendar_day_handles_leap_year_and_month_boundary() -> None:
    result = calculate_window(
        connection(),
        started_at=datetime(2024, 3, 1, 8, tzinfo=timezone.utc),
    )
    assert result.start_utc == datetime(2024, 2, 29, 5, tzinfo=timezone.utc)
    assert result.end_utc == datetime(2024, 3, 1, 5, tzinfo=timezone.utc)


def test_calendar_day_handles_year_boundary() -> None:
    result = calculate_window(
        connection(),
        started_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
    )
    assert result.start_utc == datetime(2025, 12, 31, 5, tzinfo=timezone.utc)
    assert result.end_utc == datetime(2026, 1, 1, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("started", "expected_start", "expected_end", "hours"),
    [
        (
            datetime(2024, 3, 11, 12, tzinfo=timezone.utc),
            datetime(2024, 3, 10, 5, tzinfo=timezone.utc),
            datetime(2024, 3, 11, 4, tzinfo=timezone.utc),
            23,
        ),
        (
            datetime(2024, 11, 4, 12, tzinfo=timezone.utc),
            datetime(2024, 11, 3, 4, tzinfo=timezone.utc),
            datetime(2024, 11, 4, 5, tzinfo=timezone.utc),
            25,
        ),
    ],
)
def test_calendar_day_handles_dst_boundaries(
    started: datetime,
    expected_start: datetime,
    expected_end: datetime,
    hours: int,
) -> None:
    result = calculate_window(
        connection(timezone="America/New_York"),
        started_at=started,
    )
    assert result.start_utc == expected_start
    assert result.end_utc == expected_end
    assert result.end_utc - result.start_utc == timedelta(hours=hours)


def test_rolling_hours_uses_exact_duration() -> None:
    started = datetime(2026, 7, 27, 7, tzinfo=timezone.utc)
    result = calculate_window(
        connection(window_mode=WindowMode.ROLLING_HOURS, window_hours=36),
        started_at=started,
    )
    assert result.start_utc == started - timedelta(hours=36)
    assert result.end_utc == started


def test_since_last_run_applies_overlap() -> None:
    started = datetime(2026, 7, 27, 7, tzinfo=timezone.utc)
    previous = datetime(2026, 7, 26, 8, tzinfo=timezone.utc)
    result = calculate_window(
        connection(
            window_mode=WindowMode.SINCE_LAST_RUN,
            window_overlap_min=20,
        ),
        started_at=started,
        last_successful_end_utc=previous,
    )
    assert result.start_utc == previous - timedelta(minutes=20)
    assert result.end_utc == started


def test_since_last_run_without_history_falls_back_to_window_hours() -> None:
    started = datetime(2026, 7, 27, 7, tzinfo=timezone.utc)
    result = calculate_window(
        connection(window_mode=WindowMode.SINCE_LAST_RUN, window_hours=12),
        started_at=started,
    )
    assert result.start_utc == started - timedelta(hours=12)


def test_selected_date_overrides_window_mode() -> None:
    result = calculate_window(
        connection(window_mode=WindowMode.ROLLING_HOURS),
        started_at=datetime(2026, 7, 27, 7, tzinfo=timezone.utc),
        selected_date=date(2026, 7, 10),
    )
    assert result.start_utc == datetime(2026, 7, 10, 5, tzinfo=timezone.utc)
    assert result.end_utc == datetime(2026, 7, 11, 5, tzinfo=timezone.utc)


def test_naive_clock_and_future_previous_run_are_rejected() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        calculate_window(
            connection(),
            started_at=datetime(2026, 7, 27, 7),
        )
    with pytest.raises(ValueError, match="después"):
        calculate_window(
            connection(window_mode=WindowMode.SINCE_LAST_RUN),
            started_at=datetime(2026, 7, 27, 7, tzinfo=timezone.utc),
            last_successful_end_utc=datetime(
                2026, 7, 27, 8, tzinfo=timezone.utc
            ),
        )
