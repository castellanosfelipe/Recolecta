import hashlib
import os
import threading
from collections import namedtuple
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import ConnectionRepository, Database, RunRepository
from app.downloader import (
    DownloadEngine,
    DownloadStatus,
    cleanup_orphaned_staging,
)
from app.errors import ErrorType, HarvesterError
from app.models import ConflictMode, Connection, Protocol, VerifyMode
from app.platform.secrets_fernet import FernetSecretStore
from app.transports.base import (
    ListingResult,
    RemoteFile,
    TransferResult,
    Transport,
)


CONTENT = bytes(range(256)) * 16
MODIFIED = datetime(2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc)


def connection(**changes) -> Connection:
    base = Connection(
        id=3,
        name="Descarga",
        client="Cliente",
        protocol=Protocol.SFTP,
        host="example.test",
        remote_paths=("/entrada",),
        dest_root="downloads",
        dest_template="{filename}",
        verify_mode=VerifyMode.SHA256,
        max_parallel_files=1,
        retries=2,
    )
    return replace(base, **changes).normalized()


def remote(
    path: str = "/entrada/payload.bin",
    *,
    size: int | None = len(CONTENT),
) -> RemoteFile:
    return RemoteFile(path, size, MODIFIED)


class MemoryTransport(Transport):
    def __init__(
        self,
        content: bytes,
        state: dict,
        *,
        fail_first: bool = False,
        supports_resume: bool = True,
    ) -> None:
        self.content = content
        self.state = state
        self.fail_first = fail_first
        self.supports_resume = supports_resume

    def connect(self) -> None:
        self.state["connects"] = self.state.get("connects", 0) + 1

    def close(self) -> None:
        self.state["closes"] = self.state.get("closes", 0) + 1

    def list_files(self, remote_paths, *, recursive, max_depth) -> ListingResult:
        return ListingResult()

    def stat(self, remote_path: str) -> RemoteFile:
        return remote(remote_path, size=len(self.content))

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
        self.state["calls"] = self.state.get("calls", 0) + 1
        call = self.state["calls"]
        resumed_from = offset
        if offset and not self.supports_resume:
            target.seek(0)
            target.truncate(0)
            on_restart()
            offset = 0
            resumed_from = 0
        received = 0
        for index in range(offset, len(self.content), block_size):
            chunk = self.content[index : index + block_size]
            on_chunk(chunk)
            target.write(chunk)
            received += len(chunk)
            if self.fail_first and call == 1 and received >= block_size:
                raise ConnectionResetError(10054, "corte simulado")
        self.state.setdefault("offsets", []).append(resumed_from)
        return TransferResult(
            received,
            resumed_from,
            self.supports_resume,
        )


def factory(
    state: dict,
    *,
    content: bytes = CONTENT,
    fail_first: bool = False,
    supports_resume: bool = True,
):
    return lambda: MemoryTransport(
        content,
        state,
        fail_first=fail_first,
        supports_resume=supports_resume,
    )


def test_cut_mid_download_retries_and_resumes_without_corruption(
    tmp_path: Path,
) -> None:
    state: dict = {}
    sleeps: list[float] = []
    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state, fail_first=True),
        block_size=1024,
        sleeper=sleeps.append,
        random_value=lambda: 0.0,
    )
    outcome = engine.download_files((remote(),), run_id=11)[0]
    final = tmp_path / "downloads" / "payload.bin"
    assert outcome.status == DownloadStatus.OK
    assert outcome.attempts == 2
    assert outcome.resumed_from == 1024
    assert outcome.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert final.read_bytes() == CONTENT
    assert not list((tmp_path / "downloads" / ".staging").glob("*.part"))
    assert sleeps == [1.0]
    assert int(final.stat().st_mtime) == int(MODIFIED.timestamp())


def test_instant_download_keeps_positive_duration(tmp_path: Path) -> None:
    state: dict = {}
    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
        monotonic=lambda: 100.0,
    )

    outcome = engine.download_files((remote(),), run_id=11)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.bytes_done == len(CONTENT)
    assert outcome.duration_s > 0


def test_process_restart_keeps_partial_and_restarts_if_server_has_no_resume(
    tmp_path: Path,
) -> None:
    first_state: dict = {}
    first = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(first_state, fail_first=True),
        block_size=1024,
    )
    failed = first.download_files((remote(),), run_id=12)[0]
    final = tmp_path / "downloads" / "payload.bin"
    staging = tmp_path / "downloads" / ".staging"
    assert failed.status == DownloadStatus.FAILED
    assert not final.exists()
    assert len(list(staging.glob("*.part"))) == 1
    assert list(staging.glob("*.part"))[0].stat().st_size == 1024

    second_state: dict = {}
    second = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(
            second_state,
            supports_resume=False,
        ),
        block_size=1024,
    )
    completed = second.download_files((remote(),), run_id=12)[0]
    assert completed.status == DownloadStatus.OK
    assert completed.resumed_from == 0
    assert not completed.resume_supported
    assert final.read_bytes() == CONTENT


def test_integrity_failure_never_publishes_final_name(tmp_path: Path) -> None:
    state: dict = {}
    engine = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=factory(state, content=CONTENT[:-10]),
        block_size=1024,
    )
    outcome = engine.download_files((remote(),), run_id=13)[0]
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.INTEGRITY
    assert outcome.attempts == 1
    assert not (tmp_path / "downloads" / "payload.bin").exists()
    assert len(list((tmp_path / "downloads" / ".staging").glob("*.part"))) == 1


def test_authentication_failure_is_not_retried(tmp_path: Path) -> None:
    state: dict = {}

    class AuthFailureTransport(MemoryTransport):
        def download_to(self, *args, **kwargs):
            self.state["calls"] = self.state.get("calls", 0) + 1
            raise HarvesterError(
                ErrorType.AUTH,
                "Credencial rechazada.",
                retryable=False,
            )

    engine = DownloadEngine(
        connection(retries=5),
        portable_root=tmp_path,
        transport_factory=lambda: AuthFailureTransport(CONTENT, state),
    )
    outcome = engine.download_files((remote(),), run_id=131)[0]
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.AUTH
    assert outcome.attempts == 1
    assert state["calls"] == 1


def test_cancellation_preserves_partial_for_later_resume(tmp_path: Path) -> None:
    state: dict = {}
    cancel = threading.Event()

    def progress(file, bytes_done, total):
        if bytes_done >= 1024:
            cancel.set()

    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    )
    outcome = engine.download_files(
        (remote(),),
        run_id=14,
        cancel_event=cancel,
        on_progress=progress,
    )[0]
    assert outcome.status == DownloadStatus.CANCELLED
    assert outcome.bytes_done == 1024
    assert not (tmp_path / "downloads" / "payload.bin").exists()


def test_disk_preflight_aborts_before_connecting(tmp_path: Path) -> None:
    Usage = namedtuple("Usage", "total used free")
    state: dict = {}
    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
        disk_usage=lambda path: Usage(100, 100, 0),
    )
    with pytest.raises(HarvesterError) as raised:
        engine.download_files((remote(),), run_id=15)
    assert raised.value.error_type == ErrorType.DISK_SPACE
    assert state == {}
    assert not (tmp_path / "downloads").exists()


def test_malicious_remote_path_is_failed_without_any_write(tmp_path: Path) -> None:
    state: dict = {}
    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
    )
    outcome = engine.download_files(
        (remote("../../../Windows/System32/evil.dll"),),
        run_id=16,
    )[0]
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.PATH_INVALID
    assert state == {}
    assert not (tmp_path / "downloads").exists()


def test_skip_conflict_does_not_open_transport(tmp_path: Path) -> None:
    destination = tmp_path / "downloads"
    destination.mkdir()
    final = destination / "payload.bin"
    final.write_bytes(b"existing")
    state: dict = {}
    engine = DownloadEngine(
        connection(on_conflict=ConflictMode.SKIP),
        portable_root=tmp_path,
        transport_factory=factory(state),
    )
    outcome = engine.download_files((remote(),), run_id=17)[0]
    assert outcome.status == DownloadStatus.SKIPPED
    assert final.read_bytes() == b"existing"
    assert state == {}


def test_cleanup_removes_only_unreferenced_partials(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    staging.mkdir()
    active = staging / "active.part"
    orphan = staging / "orphan.part"
    unrelated = staging / "keep.txt"
    active.write_bytes(b"a")
    orphan.write_bytes(b"b")
    unrelated.write_bytes(b"c")
    removed = cleanup_orphaned_staging(
        staging, active_part_names={active.name}
    )
    assert removed == (orphan,)
    assert active.exists()
    assert unrelated.exists()


def test_sha256_and_outcome_are_persisted_in_run_files(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "harvester.db")
    database.initialize()
    connections = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = connections.create(replace(connection(), id=None))
    runs = RunRepository(database)
    run_id = runs.start_run(
        connection_id=saved.id,
        trigger="manual",
        window_start_utc=datetime(
            2026, 7, 26, tzinfo=timezone.utc
        ),
        window_end_utc=datetime(
            2026, 7, 27, tzinfo=timezone.utc
        ),
    )
    source = remote()
    run_file_id = runs.add_file(
        run_id=run_id,
        connection_id=saved.id,
        remote_file=source,
    )
    state: dict = {}
    engine = DownloadEngine(
        saved,
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    )
    outcomes = engine.download_files(
        (source,),
        run_id=run_id,
        on_outcome=lambda outcome: runs.record_download_outcome(
            run_file_id, outcome
        ),
    )
    runs.finish_run(run_id, status="ok")
    with database.connect() as db:
        file_row = db.execute(
            "SELECT * FROM run_files WHERE id = ?", (run_file_id,)
        ).fetchone()
        run_row = db.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert outcomes[0].status == DownloadStatus.OK
    assert file_row["status"] == "ok"
    assert file_row["sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert file_row["bytes_done"] == len(CONTENT)
    assert run_row["files_downloaded"] == 1
    assert run_row["bytes_downloaded"] == len(CONTENT)
