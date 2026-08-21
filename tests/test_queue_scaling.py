"""Regression coverage for bounded discovery, queueing, and progress state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

from cryptography.fernet import Fernet
import pytest

import app.orchestrator as orchestrator_module
from app.config import AppPaths
from app.db import ConnectionRepository, Database
from app.downloader import DownloadOutcome, DownloadStatus
from app.errors import ErrorType
from app.models import Connection, Protocol, WindowMode
from app.orchestrator import PLAN_SAMPLE_LIMIT, QUEUE_BATCH_MAX, RunCoordinator
from app.platform.secrets_fernet import FernetSecretStore
from app.progress import ProgressRegistry
from app.transports.base import RemoteFile, TransferResult, Transport


FILE_COUNT = 1_205


def test_systemic_failure_signature_is_stable_but_keeps_root_cause() -> None:
    modified = datetime(2026, 7, 27, 11, tzinfo=timezone.utc)
    first = DownloadOutcome(
        RemoteFile("/entrada/lote-1.bin", 101, modified),
        DownloadStatus.FAILED,
        None,
        attempts=1,
        bytes_done=51,
        error_type=ErrorType.PARTIAL_TRANSFER,
        error_msg=(
            "La transferencia de /entrada/lote-1.bin terminó en 51 bytes; "
            "se esperaban 101 bytes."
        ),
    )
    second = DownloadOutcome(
        RemoteFile("/entrada/lote-2.bin", 202, modified),
        DownloadStatus.FAILED,
        None,
        attempts=1,
        bytes_done=87,
        error_type=ErrorType.PARTIAL_TRANSFER,
        error_msg=(
            "La transferencia de /entrada/lote-2.bin terminó en 87 bytes; "
            "se esperaban 202 bytes."
        ),
    )
    distinct = DownloadOutcome(
        RemoteFile("/entrada/lote-3.bin", 303, modified),
        DownloadStatus.FAILED,
        None,
        attempts=1,
        bytes_done=19,
        error_type=ErrorType.PARTIAL_TRANSFER,
        error_msg=(
            "El servidor cerró el canal de /entrada/lote-3.bin tras "
            "19 bytes; se esperaban 303 bytes."
        ),
    )

    first_signature = orchestrator_module._systemic_failure_signature(first)
    assert first_signature == (
        orchestrator_module._systemic_failure_signature(second)
    )
    assert first_signature != (
        orchestrator_module._systemic_failure_signature(distinct)
    )


class LazyInventoryTransport(Transport):
    """Expose a large inventory and prove batches persist before later yields."""

    def __init__(
        self,
        database: Database,
        *,
        modified_at: datetime,
        file_count: int,
    ) -> None:
        self.database = database
        self.modified_at = modified_at
        self.file_count = file_count
        self.connected = False
        self.closed = False
        self.yielded = 0
        self.saw_first_batch_persisted = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ):
        assert self.connected
        assert remote_paths == ("/entrada",)
        for index in range(self.file_count):
            if index == 500:
                with self.database.connect() as connection:
                    persisted = connection.execute(
                        "SELECT COUNT(*) FROM run_files"
                    ).fetchone()[0]
                assert persisted == 500
                self.saw_first_batch_persisted = True
            self.yielded += 1
            yield RemoteFile(
                f"/entrada/lote/{index:05d}.bin",
                1,
                self.modified_at,
            )

    def stat(self, remote_path: str) -> RemoteFile:
        raise AssertionError("La descarga simulada no debe consultar stat.")

    def download_to(
        self,
        remote_path,
        target,
        *,
        offset,
        block_size,
        on_chunk,
        on_restart,
    ) -> TransferResult:
        raise AssertionError(
            "La prueba sustituye el motor para aislar el tamaño de los lotes."
        )


class PausedInventoryTransport(LazyInventoryTransport):
    """Pause before a second batch so live discovery state can be inspected."""

    def __init__(self, *args, pause_at: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pause_at = pause_at
        self.paused = threading.Event()
        self.release = threading.Event()

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ):
        assert self.connected
        assert remote_paths == ("/entrada",)
        for index in range(self.file_count):
            if index == self.pause_at:
                self.paused.set()
                if not self.release.wait(timeout=10):
                    raise TimeoutError("La prueba no liberó el inventario.")
            self.yielded += 1
            yield RemoteFile(
                f"/entrada/lote/{index:05d}.bin",
                1,
                self.modified_at,
            )


def test_live_discovery_publishes_each_persisted_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    saved = connections.create(
        Connection(
            name="Gesdoc",
            protocol=Protocol.FTP,
            host="example.test",
            remote_paths=("/entrada",),
            recursive=True,
            dest_root="downloads",
            dest_template="{remote_tree}",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=24,
            quiet_period_s=0,
        )
    )
    batch_size = 3
    listing_transport = PausedInventoryTransport(
        database,
        modified_at=now - timedelta(hours=1),
        file_count=batch_size + 1,
        pause_at=batch_size,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "DISCOVERY_BATCH_SIZE",
        batch_size,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: listing_transport,
    )

    def download_batch(
        self,
        files,
        *,
        run_id,
        cancel_event=None,
        on_progress=None,
        on_outcome=None,
        destination_paths=None,
        replace_existing=False,
        check_disk_space=True,
    ):
        del (
            self,
            run_id,
            cancel_event,
            on_progress,
            replace_existing,
            check_disk_space,
        )
        outcomes = tuple(
            DownloadOutcome(
                remote_file,
                DownloadStatus.OK,
                destination_paths[remote_file.identity],
                attempts=1,
                bytes_done=remote_file.size_bytes or 0,
                duration_s=0.001,
            )
            for remote_file in files
        )
        if on_outcome is not None:
            for outcome in outcomes:
                on_outcome(outcome)
        return outcomes

    monkeypatch.setattr(
        orchestrator_module.DownloadEngine,
        "download_files",
        download_batch,
    )
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    )
    worker = coordinator.submit_connection(saved.id, trigger="manual")
    try:
        assert listing_transport.paused.wait(timeout=10)
        with database.connect() as connection:
            run = dict(
                connection.execute(
                    "SELECT * FROM runs WHERE status = 'running'"
                ).fetchone()
            )
        snapshot = coordinator.progress.snapshot()["runs"][0]

        assert run["phase"] == "discovering"
        assert run["files_found"] == batch_size
        assert run["files_planned"] == batch_size
        assert run["planned_bytes"] == batch_size
        assert snapshot["phase"] == "discovering"
        assert snapshot["files_discovered"] == batch_size
        assert snapshot["files_planned"] == batch_size
        assert snapshot["planned_bytes"] == batch_size
    finally:
        listing_transport.release.set()
        worker.join(timeout=10)

    assert worker.is_alive() is False
    with database.connect() as connection:
        finished = connection.execute(
            "SELECT status, files_found FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert finished["status"] == "ok"
    assert finished["files_found"] == batch_size + 1


def test_large_lazy_inventory_uses_bounded_samples_and_queue_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    saved = connections.create(
        Connection(
            name="Inventario masivo",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            recursive=True,
            dest_root="downloads",
            dest_template="{remote_tree}",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=24,
            quiet_period_s=0,
            max_parallel_files=40,
        )
    )
    listing_transport = LazyInventoryTransport(
        database,
        modified_at=now - timedelta(hours=1),
        file_count=FILE_COUNT,
    )
    factory_calls = 0

    def create_transport(connection, secret, known_hosts):
        nonlocal factory_calls
        factory_calls += 1
        return listing_transport

    download_batch_sizes: list[int] = []

    def download_batch(
        self,
        files,
        *,
        run_id,
        cancel_event=None,
        on_progress=None,
        on_outcome=None,
        destination_paths=None,
        replace_existing=False,
        check_disk_space=True,
    ):
        download_batch_sizes.append(len(files))
        assert destination_paths is not None
        assert check_disk_space is False
        outcomes = []
        for remote_file in files:
            outcome = DownloadOutcome(
                remote_file,
                DownloadStatus.OK,
                destination_paths[remote_file.identity],
                attempts=1,
                bytes_done=remote_file.size_bytes or 0,
                duration_s=0.001,
            )
            outcomes.append(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
        return tuple(outcomes)

    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        create_transport,
    )
    monkeypatch.setattr(
        orchestrator_module.DownloadEngine,
        "download_files",
        download_batch,
    )

    execution = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    ).execute_connection(saved.id, trigger="manual")

    assert execution.status == "ok"
    assert execution.run_id is not None
    assert execution.plan.files_found_count == FILE_COUNT
    assert execution.plan.files_to_download_count == FILE_COUNT
    assert len(execution.plan.items) == PLAN_SAMPLE_LIMIT == 500
    assert execution.plan.items_truncated is True
    assert execution.plan.counters["planned"] == FILE_COUNT
    assert len(execution.outcomes) == PLAN_SAMPLE_LIMIT
    assert execution.outcomes_truncated is True

    with database.connect() as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM run_files WHERE run_id = ?",
            (execution.run_id,),
        ).fetchone()[0]
        completed = connection.execute(
            """
            SELECT COUNT(*) FROM run_files
            WHERE run_id = ? AND status = 'ok'
            """,
            (execution.run_id,),
        ).fetchone()[0]
    assert persisted == FILE_COUNT
    assert completed == FILE_COUNT

    assert listing_transport.saw_first_batch_persisted is True
    assert listing_transport.yielded == FILE_COUNT
    assert listing_transport.closed is True
    assert factory_calls == 1
    assert sum(download_batch_sizes) == FILE_COUNT
    assert max(download_batch_sizes) == QUEUE_BATCH_MAX == 64
    assert all(0 < size <= QUEUE_BATCH_MAX for size in download_batch_sizes)


@pytest.mark.parametrize(
    "systemic_error_type",
    (
        ErrorType.AUTH,
        ErrorType.PERMISSION,
        ErrorType.PROTOCOL,
        ErrorType.PARTIAL_TRANSFER,
        ErrorType.UNKNOWN,
    ),
)
def test_systemic_failures_open_circuit_and_terminalize_large_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    systemic_error_type: ErrorType,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    saved = connections.create(
        Connection(
            name="Origen sin credenciales",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            recursive=True,
            dest_root="downloads",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=24,
            quiet_period_s=0,
            max_parallel_files=40,
            retries=0,
        )
    )
    listing_transport = LazyInventoryTransport(
        database,
        modified_at=now - timedelta(hours=1),
        file_count=FILE_COUNT,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: listing_transport,
    )
    attempted_batch_sizes: list[int] = []

    def fail_download_batch(
        self,
        files,
        *,
        run_id,
        cancel_event=None,
        on_progress=None,
        on_outcome=None,
        destination_paths=None,
        replace_existing=False,
        check_disk_space=True,
    ):
        del (
            self,
            run_id,
            cancel_event,
            on_progress,
            destination_paths,
            replace_existing,
            check_disk_space,
        )
        attempted_batch_sizes.append(len(files))
        outcomes = tuple(
            DownloadOutcome(
                remote_file,
                DownloadStatus.FAILED,
                None,
                attempts=1,
                bytes_done=(remote_file.size_bytes or 0) // 2,
                error_type=systemic_error_type,
                error_msg=(
                    "Fallo sistémico simulado para "
                    f"{remote_file.remote_path}; recibidos "
                    f"{(remote_file.size_bytes or 0) // 2} de "
                    f"{remote_file.size_bytes} bytes."
                ),
            )
            for remote_file in files
        )
        if on_outcome is not None:
            for outcome in outcomes:
                on_outcome(outcome)
        return outcomes

    monkeypatch.setattr(
        orchestrator_module.DownloadEngine,
        "download_files",
        fail_download_batch,
    )

    execution = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    ).execute_connection(saved.id, trigger="manual")

    assert execution.status == "failed"
    assert attempted_batch_sizes == [QUEUE_BATCH_MAX, QUEUE_BATCH_MAX]
    assert sum(attempted_batch_sizes) < FILE_COUNT
    assert execution.outcome_counts["failed"] == FILE_COUNT
    assert execution.outcomes_truncated is True
    with database.connect() as connection:
        run = connection.execute(
            "SELECT * FROM runs WHERE id = ?",
            (execution.run_id,),
        ).fetchone()
        statuses = connection.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM run_files
            WHERE run_id = ?
            GROUP BY status
            """,
            (execution.run_id,),
        ).fetchall()
    assert run["error_type"] == systemic_error_type.value
    assert "cola se detuvo de forma preventiva" in run["error_msg"]
    assert run["files_failed"] == FILE_COUNT
    assert {row["status"]: row["total"] for row in statuses} == {
        "failed": FILE_COUNT
    }


def test_bounded_progress_discards_terminal_file_details() -> None:
    registry = ProgressRegistry()
    registry.start_run(
        run_id=90,
        connection_id=7,
        connection_name="Cola acotada",
        trigger="manual",
        files=(),
        bounded=True,
        phase="downloading",
        total_files=FILE_COUNT,
        total_size_bytes=FILE_COUNT,
    )
    first_batch = tuple(
        (
            index + 1,
            RemoteFile(
                f"/entrada/{index:05d}.bin",
                1,
                datetime(2026, 7, 27, tzinfo=timezone.utc),
            ),
        )
        for index in range(QUEUE_BATCH_MAX)
    )
    registry.add_files(90, first_batch)

    for run_file_id, remote_file in first_batch:
        registry.finish_file(
            90,
            run_file_id,
            DownloadOutcome(
                remote_file,
                DownloadStatus.OK,
                Path("downloads") / remote_file.name,
                attempts=1,
                bytes_done=1,
            ),
        )

    snapshot = registry.snapshot()["runs"][0]
    assert snapshot["files"] == []
    assert snapshot["files_total"] == FILE_COUNT
    assert snapshot["files_completed"] == QUEUE_BATCH_MAX
    assert snapshot["statuses"] == {"ok": QUEUE_BATCH_MAX}
    assert snapshot["bytes_done"] == QUEUE_BATCH_MAX

    next_file = RemoteFile(
        "/entrada/siguiente.bin",
        1,
        datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    registry.add_files(90, ((QUEUE_BATCH_MAX + 1, next_file),))
    snapshot = registry.snapshot()["runs"][0]
    assert len(snapshot["files"]) == 1
    assert snapshot["files"][0]["status"] == "pending"
    assert snapshot["statuses"] == {
        "ok": QUEUE_BATCH_MAX,
        "pending": 1,
    }


def test_large_skipped_inventory_persists_only_bounded_audit_sample(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    saved = connections.create(
        Connection(
            name="Inventario histórico",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            recursive=True,
            dest_root="downloads",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=1,
            quiet_period_s=0,
        )
    )
    listing_transport = LazyInventoryTransport(
        database,
        modified_at=now - timedelta(days=30),
        file_count=FILE_COUNT,
    )
    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: listing_transport,
    )

    execution = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    ).execute_connection(saved.id, trigger="manual")

    with database.connect() as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM run_files WHERE run_id = ?",
            (execution.run_id,),
        ).fetchone()[0]
        run = connection.execute(
            "SELECT * FROM runs WHERE id = ?",
            (execution.run_id,),
        ).fetchone()
    assert execution.status == "ok"
    assert execution.plan.files_found_count == FILE_COUNT
    assert execution.plan.files_to_download_count == 0
    assert execution.plan.items_truncated is True
    assert persisted == PLAN_SAMPLE_LIMIT
    assert run["files_found"] == FILE_COUNT
    assert run["files_skipped"] == FILE_COUNT
