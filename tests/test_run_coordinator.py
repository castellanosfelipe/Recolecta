from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import ftplib

from cryptography.fernet import Fernet
import pytest

import app.orchestrator as orchestrator_module
from app.config import AppPaths
from app.db import ConnectionRepository, Database
from app.models import Connection, Protocol, VerifyMode, WindowMode
from app.orchestrator import PlanStatus, RunCoordinator
from app.platform.secrets_fernet import FernetSecretStore
from app.transports.base import (
    ListingResult,
    RemoteFile,
    TransferResult,
    Transport,
)


def test_coordinator_plans_downloads_persists_and_deduplicates(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    content = b"coordinator-content"
    source = RemoteFile(
        "/entrada/report.csv",
        len(content),
        now - timedelta(hours=2),
    )
    state = {"downloads": 0}

    class MemoryTransport(Transport):
        def connect(self):
            return None

        def close(self):
            return None

        def list_files(self, remote_paths, *, recursive, max_depth):
            return ListingResult((source,))

        def stat(self, remote_path):
            return source

        def download_to(
            self,
            remote_path,
            target,
            *,
            offset,
            block_size,
            on_chunk,
            on_restart,
        ):
            state["downloads"] += 1
            chunk = content[offset:]
            on_chunk(chunk)
            target.write(chunk)
            return TransferResult(len(chunk), offset, True)

    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: MemoryTransport(),
    )
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(
            name="Coordinada",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            dest_root="downloads",
            dest_template="{filename}",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=24,
            verify_mode=VerifyMode.SHA256,
        )
    )
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    )
    execution = coordinator.execute_connection(
        saved.id,
        trigger="cli",
    )
    assert execution.status == "ok"
    assert execution.run_id is not None
    assert state["downloads"] == 1
    assert (tmp_path / "downloads" / "report.csv").read_bytes() == content
    with database.connect() as db:
        run = db.execute(
            "SELECT * FROM runs WHERE id = ?", (execution.run_id,)
        ).fetchone()
        run_file = db.execute(
            "SELECT * FROM run_files WHERE run_id = ?", (execution.run_id,)
        ).fetchone()
    assert run["status"] == "ok"
    assert run_file["status"] == "ok"
    assert run_file["sha256"]
    assert run_file["average_bps"] > 0
    log_path = next(paths.run_logs.glob(f"*_{execution.run_id}.jsonl"))
    events = [
        json.loads(line)["event"]
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0] == "run_started"
    assert "file_planned" in events
    assert "file_started" in events
    assert "file_progress" in events
    assert events.count("file_progress") == 10
    assert "file_done" in events
    assert events[-1] == "run_finished"

    dry = coordinator.execute_connection(
        saved.id,
        trigger="cli",
        dry_run_only=True,
    )
    assert dry.status == "dry_run"
    assert dry.plan.items[0].status == PlanStatus.DUPLICATE
    assert state["downloads"] == 1
    scheduled_again = coordinator.execute_connection(
        saved.id,
        trigger="schedule",
    )
    assert scheduled_again.status == "already_completed"
    assert scheduled_again.run_id is None
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_valid_empty_listing_is_success_with_no_files_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
    transports: list[Transport] = []

    class EmptyTransport(Transport):
        def __init__(self) -> None:
            self.closed = False

        def connect(self):
            return None

        def close(self):
            self.closed = True

        def list_files(self, remote_paths, *, recursive, max_depth):
            return ListingResult(())

        def stat(self, remote_path):
            raise AssertionError("No debe consultar archivos inexistentes.")

        def download_to(self, *args, **kwargs):
            raise AssertionError("No debe iniciar una descarga sin archivos.")

    def create_empty_transport(connection, secret, known_hosts):
        transport = EmptyTransport()
        transports.append(transport)
        return transport

    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        create_empty_transport,
    )
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(
            name="Sin archivos",
            host="example.test",
            remote_paths=("/entrada",),
            window_mode=WindowMode.ROLLING_HOURS,
        )
    )
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    )

    execution = coordinator.execute_connection(saved.id, trigger="manual")
    assert execution.run_id is not None
    persisted = coordinator.runs.get_run(execution.run_id)
    summary = execution.summary()

    assert execution.status == "ok"
    assert persisted["status"] == "ok"
    assert persisted["files_found"] == 0
    assert persisted["files_failed"] == 0
    assert summary["status"] == "ok"
    assert summary["result_status"] == "no_files"
    assert summary["status_label"] == "Archivos no existentes"
    assert transports
    assert all(transport.closed for transport in transports)


def test_cancellation_closes_partially_consumed_remote_listing(
    monkeypatch,
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
            name="Listado cancelable",
            protocol=Protocol.SFTP,
            host="example.test",
            remote_paths=("/entrada",),
            dest_root="downloads",
            window_mode=WindowMode.ROLLING_HOURS,
            window_hours=24,
            quiet_period_s=0,
        )
    )
    state = {"iterator_closed": False, "transport_closed": False}
    coordinator_holder: list[RunCoordinator] = []

    class CancellableListingTransport(Transport):
        def connect(self):
            return None

        def close(self):
            state["transport_closed"] = True

        def iter_files(self, remote_paths, *, recursive, max_depth):
            del remote_paths, recursive, max_depth
            self._reset_listing_warnings()

            def stream():
                try:
                    yield RemoteFile(
                        "/entrada/primero.bin",
                        1,
                        now - timedelta(hours=1),
                    )
                    with database.connect() as connection:
                        run_id = int(
                            connection.execute(
                                """
                                SELECT id FROM runs
                                WHERE status = 'running'
                                ORDER BY id DESC LIMIT 1
                                """
                            ).fetchone()["id"]
                        )
                    assert coordinator_holder[0].cancel(run_id) is True
                    yield RemoteFile(
                        "/entrada/segundo.bin",
                        1,
                        now - timedelta(hours=1),
                    )
                    raise AssertionError(
                        "La cancelación no debe seguir consumiendo el listado."
                    )
                finally:
                    state["iterator_closed"] = True

            return stream()

        def stat(self, remote_path):
            raise AssertionError

        def download_to(self, *args, **kwargs):
            raise AssertionError

    transport = CancellableListingTransport()
    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: transport,
    )
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    )
    coordinator_holder.append(coordinator)

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    assert execution.status == "cancelled"
    assert execution.plan.files_found_count == 0
    assert state == {
        "iterator_closed": True,
        "transport_closed": True,
    }


def test_coordinator_persists_and_redacts_listing_failure(
    monkeypatch, tmp_path: Path
) -> None:
    now = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)

    class BrokenTransport(Transport):
        def connect(self):
            return None

        def close(self):
            return None

        def list_files(self, remote_paths, *, recursive, max_depth):
            raise ftplib.error_perm("530 password=credencial-real")

        def stat(self, remote_path):
            raise AssertionError

        def download_to(self, *args, **kwargs):
            raise AssertionError

    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: BrokenTransport(),
    )
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(
        Connection(
            name="Credencial",
            host="example.test",
            remote_paths=("/entrada",),
            window_mode=WindowMode.ROLLING_HOURS,
        )
    )
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: now,
    )
    with pytest.raises(ftplib.error_perm):
        coordinator.execute_connection(saved.id, trigger="manual")
    with database.connect() as db:
        run = db.execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "failed"
    assert run["error_type"] == "auth"
    assert "credencial-real" not in run["error_msg"]
    assert "password=***" in run["error_msg"]
    log_text = next(paths.run_logs.glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    assert "credencial-real" not in log_text
