from datetime import datetime, timezone

from app.downloader import DownloadOutcome, DownloadStatus
from app.progress import ProgressRegistry
from app.transports.base import RemoteFile


def test_progress_snapshot_speed_eta_and_persistence_rate() -> None:
    clock = [10.0]
    persisted: list[tuple[int, int]] = []
    remote = RemoteFile("/entrada/reporte.csv", 1_000, None)
    registry = ProgressRegistry(
        persist_progress=lambda file_id, done: persisted.append((file_id, done)),
        monotonic=lambda: clock[0],
        wall_clock=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    registry.start_run(
        run_id=4,
        connection_id=2,
        connection_name="Ventas",
        trigger="manual",
        files=((8, remote),),
    )

    clock[0] = 10.2
    registry.update_file(4, 8, 100, worker="worker-1")
    clock[0] = 10.8
    registry.update_file(4, 8, 400, worker="worker-1")
    assert persisted == []

    clock[0] = 11.2
    registry.update_file(4, 8, 600, worker="worker-1")
    snapshot = registry.snapshot()
    file = snapshot["runs"][0]["files"][0]
    assert persisted == [(8, 600)]
    assert snapshot["active"] is True
    assert file["percent"] == 60.0
    assert file["average_bps"] == 600.0
    assert file["eta_s"] == 0.7
    assert file["worker"] == "worker-1"

    clock[0] = 11.9
    registry.update_file(4, 8, 800)
    assert persisted == [(8, 600)]
    clock[0] = 12.2
    registry.update_file(4, 8, 900)
    assert persisted == [(8, 600), (8, 900)]


def test_progress_cancel_finish_and_unknown_size() -> None:
    clock = [0.0]
    remote = RemoteFile("/entrada/sin-tamano.bin", None, None)
    registry = ProgressRegistry(monotonic=lambda: clock[0])
    registry.start_run(
        run_id=5,
        connection_id=3,
        connection_name="Archivo",
        trigger="schedule",
        files=((9, remote),),
    )
    clock[0] = 2.0
    registry.update_file(5, 9, 512)
    assert registry.mark_cancel_requested(5) is True
    snapshot = registry.snapshot()["runs"][0]
    assert snapshot["cancel_requested"] is True
    assert snapshot["percent"] is None
    assert snapshot["eta_s"] is None

    registry.finish_file(
        5,
        9,
        DownloadOutcome(
            remote,
            DownloadStatus.CANCELLED,
            None,
            attempts=1,
            bytes_done=512,
        ),
    )
    assert registry.snapshot()["runs"][0]["statuses"] == {"cancelled": 1}
    registry.finish_run(5)
    assert registry.snapshot() == {
        "active": False,
        "active_runs": 0,
        "runs": [],
    }
