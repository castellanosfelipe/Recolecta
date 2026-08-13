import errno
import hashlib
import os
import threading
import uuid
from collections import namedtuple
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db import ConnectionRepository, Database, RunRepository
from app.downloader import (
    DownloadEngine,
    DownloadStatus,
    cleanup_orphaned_staging,
    estimate_download_bytes,
)
from app.errors import ErrorType, RecolectaError
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
        retry_waiter=lambda event, delay: (
            sleeps.append(delay) or event.is_set()
        ),
    )
    outcome = engine.download_files((remote(),), run_id=11)[0]
    final = tmp_path / "downloads" / "payload.bin"
    assert outcome.status == DownloadStatus.OK
    assert outcome.attempts == 2
    assert outcome.resumed_from == 1024
    assert outcome.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert final.read_bytes() == CONTENT
    assert not list((tmp_path / "downloads" / ".staging").rglob("*.part"))
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


def test_managed_engine_reuses_one_transport_session_across_batches(
    tmp_path: Path,
) -> None:
    state: dict = {}
    with DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ) as engine:
        first = engine.download_files(
            (remote("/entrada/first.bin"),),
            run_id=20,
        )
        second = engine.download_files(
            (remote("/entrada/second.bin"),),
            run_id=20,
        )
        assert state["connects"] == 1
        assert state.get("closes", 0) == 0

    assert first[0].status == DownloadStatus.OK
    assert second[0].status == DownloadStatus.OK
    assert state["connects"] == 1
    assert state["closes"] == 1


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
    identity = "|".join(
        ("3", "/entrada/payload.bin", MODIFIED.isoformat(), str(len(CONTENT)))
    )
    expected_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    expected_part = staging / expected_name[:2] / expected_name
    assert list(staging.rglob("*.part")) == [expected_part]
    assert expected_part.stat().st_size == 1024

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


def test_partial_transfer_retries_and_never_publishes_short_file(
    tmp_path: Path,
) -> None:
    state: dict = {}
    engine = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=factory(state, content=CONTENT[:-10]),
        block_size=1024,
        sleeper=lambda delay: None,
        random_value=lambda: 0.0,
        retry_waiter=lambda event, delay: event.is_set(),
    )
    outcome = engine.download_files((remote(),), run_id=13)[0]
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.PARTIAL_TRANSFER
    assert outcome.attempts == 4
    assert not (tmp_path / "downloads" / "payload.bin").exists()
    assert len(list((tmp_path / "downloads" / ".staging").rglob("*.part"))) == 1


def test_oversized_transfer_is_integrity_failure_without_retry(
    tmp_path: Path,
) -> None:
    state: dict = {}
    engine = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=factory(state, content=CONTENT + b"x"),
        block_size=1024,
    )

    outcome = engine.download_files((remote(),), run_id=130)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.INTEGRITY
    assert outcome.attempts == 1
    assert not (tmp_path / "downloads" / "payload.bin").exists()
    partial = next((tmp_path / "downloads" / ".staging").rglob("*.part"))
    assert partial.stat().st_size <= len(CONTENT)


def test_oversized_chunk_is_rejected_before_consuming_disk_or_bandwidth(
    tmp_path: Path,
) -> None:
    state: dict = {}
    bandwidth = type(
        "BandwidthProbe",
        (),
        {
            "consume": lambda self, amount, *, cancel_event=None: (
                state.setdefault("bandwidth", []).append(amount) or True
            )
        },
    )()
    engine = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state, content=CONTENT + b"x"),
        block_size=8192,
    )
    engine.bandwidth_buckets = (bandwidth,)

    outcome = engine.download_files((remote(),), run_id=131)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.INTEGRITY
    assert state.get("bandwidth", []) == []
    partial = next((tmp_path / "downloads" / ".staging").rglob("*.part"))
    assert partial.stat().st_size == 0


def test_authentication_failure_is_not_retried(tmp_path: Path) -> None:
    state: dict = {}

    class AuthFailureTransport(MemoryTransport):
        def download_to(self, *args, **kwargs):
            self.state["calls"] = self.state.get("calls", 0) + 1
            raise RecolectaError(
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


def test_explicit_retryable_error_is_retried_even_for_protocol_category(
    tmp_path: Path,
) -> None:
    state: dict = {}

    class RetryableProtocolTransport(MemoryTransport):
        def download_to(self, *args, **kwargs):
            self.state["calls"] = self.state.get("calls", 0) + 1
            if self.state["calls"] == 1:
                raise RecolectaError(
                    ErrorType.PROTOCOL,
                    "Interrupción transitoria simulada.",
                    retryable=True,
                )
            return super().download_to(*args, **kwargs)

    outcome = DownloadEngine(
        connection(retries=2),
        portable_root=tmp_path,
        transport_factory=lambda: RetryableProtocolTransport(CONTENT, state),
        retry_waiter=lambda event, delay: event.is_set(),
    ).download_files((remote(),), run_id=133)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.attempts == 2
    assert state["calls"] == 3


def test_non_retryable_error_overrides_a_retryable_category(tmp_path: Path) -> None:
    state: dict = {}

    class PermanentTimeoutTransport(MemoryTransport):
        def download_to(self, *args, **kwargs):
            self.state["calls"] = self.state.get("calls", 0) + 1
            raise RecolectaError(
                ErrorType.TCP_TIMEOUT,
                "Timeout permanente simulado.",
                retryable=False,
            )

    outcome = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=lambda: PermanentTimeoutTransport(CONTENT, state),
    ).download_files((remote(),), run_id=134)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.attempts == 1
    assert state["calls"] == 1


def test_cancellation_interrupts_retry_backoff(tmp_path: Path) -> None:
    state: dict = {}
    cancel = threading.Event()
    waits: list[float] = []

    class TimeoutTransport(MemoryTransport):
        def download_to(self, *args, **kwargs):
            self.state["calls"] = self.state.get("calls", 0) + 1
            raise TimeoutError("servidor sin respuesta")

    def cancel_during_wait(
        event: threading.Event,
        delay: float,
    ) -> bool:
        waits.append(delay)
        event.set()
        return True

    outcome = DownloadEngine(
        connection(retries=5),
        portable_root=tmp_path,
        transport_factory=lambda: TimeoutTransport(CONTENT, state),
        random_value=lambda: 0.0,
        retry_waiter=cancel_during_wait,
    ).download_files(
        (remote(),),
        run_id=132,
        cancel_event=cancel,
    )[0]

    assert outcome.status == DownloadStatus.CANCELLED
    assert outcome.attempts == 1
    assert waits == [1.0]
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
    with pytest.raises(RecolectaError) as raised:
        engine.download_files((remote(),), run_id=15)
    assert raised.value.error_type == ErrorType.DISK_SPACE
    assert state == {}
    assert not (tmp_path / "downloads").exists()


def test_unknown_size_discards_stale_legacy_partial_and_restarts(
    tmp_path: Path,
) -> None:
    source = remote(size=None)
    staging = tmp_path / "downloads" / ".staging"
    staging.mkdir(parents=True)
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), "None")
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    legacy = staging / part_name
    legacy.write_bytes(b"contenido obsoleto que no pertenece al remoto")
    state: dict = {}

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((source,), run_id=151)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == 0
    assert state["offsets"] == [0]
    assert not legacy.exists()
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


def test_unknown_size_preflight_requires_configured_floor_plus_reserve(
    tmp_path: Path,
) -> None:
    Usage = namedtuple("Usage", "total used free")
    state: dict = {}
    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory(state),
        unknown_size_reserve_bytes=1024,
        disk_usage=lambda path: Usage(10_000, 8_976, 1024),
    )

    with pytest.raises(RecolectaError) as raised:
        engine.download_files((remote(size=None),), run_id=152)

    assert raised.value.error_type == ErrorType.DISK_SPACE
    assert "requeridos 1126" in str(raised.value)
    assert state == {}
    assert not (tmp_path / "downloads").exists()


def test_unknown_size_preflight_is_bounded_by_active_slots(
    tmp_path: Path,
) -> None:
    Usage = namedtuple("Usage", "total used free")
    state: dict = {}
    probes: list[Path] = []

    def disk_usage(path: Path):
        probes.append(path)
        return Usage(10_000, 7_748, 2_252)

    sources = tuple(
        remote(f"/entrada/payload-{index}.bin", size=None)
        for index in range(10)
    )
    outcomes = DownloadEngine(
        connection(max_parallel_files=2),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
        unknown_size_reserve_bytes=1024,
        disk_space_check_interval_bytes=1024,
        disk_usage=disk_usage,
    ).download_files(sources, run_id=155)

    assert len(outcomes) == len(sources)
    assert all(outcome.status == DownloadStatus.OK for outcome in outcomes)
    assert probes
    for source in sources:
        local = tmp_path / "downloads" / Path(source.remote_path).name
        assert local.read_bytes() == CONTENT


def test_unknown_size_stream_stops_before_consuming_disk_reserve(
    tmp_path: Path,
) -> None:
    Usage = namedtuple("Usage", "total used free")
    state: dict = {}
    free_samples = iter((10_000, 2_251))
    probes: list[int] = []

    def disk_usage(path: Path):
        free = next(free_samples)
        probes.append(free)
        return Usage(10_000, 10_000 - free, free)

    outcome = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
        unknown_size_reserve_bytes=2048,
        disk_space_check_interval_bytes=2048,
        disk_usage=disk_usage,
    ).download_files(
        (remote(size=None),),
        run_id=156,
        check_disk_space=False,
    )[0]

    partials = list((tmp_path / "downloads" / ".staging").rglob("*.part"))
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.DISK_SPACE
    assert outcome.attempts == 1
    assert outcome.bytes_done == 2048
    assert probes == [10_000, 2_251]
    assert state["calls"] == 1
    assert not (tmp_path / "downloads" / "payload.bin").exists()
    assert len(partials) == 1
    assert partials[0].read_bytes() == CONTENT[:2048]


def test_unknown_size_retry_restarts_and_preserves_binary_content(
    tmp_path: Path,
) -> None:
    Usage = namedtuple("Usage", "total used free")
    state: dict = {}
    outcome = DownloadEngine(
        connection(retries=1),
        portable_root=tmp_path,
        transport_factory=factory(state, fail_first=True),
        block_size=1024,
        unknown_size_reserve_bytes=1024,
        disk_space_check_interval_bytes=1024,
        disk_usage=lambda path: Usage(100_000, 0, 100_000),
        retry_waiter=lambda event, delay: event.is_set(),
    ).download_files((remote(size=None),), run_id=158)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.attempts == 2
    assert outcome.resumed_from == 0
    assert state["calls"] == 2
    assert state["offsets"] == [0]
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


@pytest.mark.parametrize(
    ("mtime_utc", "timestamp_reliable"),
    (
        (None, True),
        (MODIFIED, False),
    ),
)
def test_known_size_without_trustworthy_timestamp_restarts_partial(
    mtime_utc: datetime | None,
    timestamp_reliable: bool,
    tmp_path: Path,
) -> None:
    source = RemoteFile(
        "/entrada/payload.bin",
        len(CONTENT),
        mtime_utc,
        timestamp_reliable=timestamp_reliable,
    )
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        (
            "3",
            source.remote_path,
            mtime_utc.isoformat() if mtime_utc else "",
            str(len(CONTENT)),
        )
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    partial = staging / part_name[:2] / part_name
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"\xff" * 1024)
    state: dict = {}

    assert (
        estimate_download_bytes(
            tmp_path / "downloads",
            source,
            connection=connection(),
        )
        == len(CONTENT)
    )

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((source,), run_id=157)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == 0
    assert state["offsets"] == [0]
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


def test_resume_rejects_remote_replacement_with_same_size(tmp_path: Path) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    partial = staging / part_name[:2] / part_name
    partial.parent.mkdir(parents=True)
    partial.write_bytes(CONTENT[:1024])
    state: dict = {}

    class ReplacedRemoteTransport(MemoryTransport):
        def stat(self, remote_path: str) -> RemoteFile:
            return RemoteFile(
                remote_path,
                len(self.content),
                MODIFIED + timedelta(seconds=1),
            )

    outcome = DownloadEngine(
        connection(retries=2),
        portable_root=tmp_path,
        transport_factory=lambda: ReplacedRemoteTransport(CONTENT, state),
    ).download_files((source,), run_id=159)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.INTEGRITY
    assert outcome.attempts == 1
    assert state.get("calls", 0) == 0
    assert partial.read_bytes() == b""
    assert not (tmp_path / "downloads" / "payload.bin").exists()


def test_resume_size_change_never_falls_through_to_unchecked_download(
    tmp_path: Path,
) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    partial = staging / part_name[:2] / part_name
    partial.parent.mkdir(parents=True)
    partial.write_bytes(CONTENT[:1024])
    state: dict = {}

    class ResizedRemoteTransport(MemoryTransport):
        def stat(self, remote_path: str) -> RemoteFile:
            self.state["stats"] = self.state.get("stats", 0) + 1
            return RemoteFile(remote_path, len(self.content) + 1, MODIFIED)

    outcome = DownloadEngine(
        connection(retries=3),
        portable_root=tmp_path,
        transport_factory=lambda: ResizedRemoteTransport(CONTENT, state),
    ).download_files((source,), run_id=161)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.PARTIAL_TRANSFER
    assert outcome.attempts == 4
    assert state["stats"] == 4
    assert state.get("calls", 0) == 0
    assert partial.read_bytes() == CONTENT[:1024]
    assert not (tmp_path / "downloads" / "payload.bin").exists()


def test_resume_revalidates_remote_again_before_publish(tmp_path: Path) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    partial = staging / part_name[:2] / part_name
    partial.parent.mkdir(parents=True)
    partial.write_bytes(CONTENT[:1024])
    state: dict = {}

    class ChangesDuringResumeTransport(MemoryTransport):
        def stat(self, remote_path: str) -> RemoteFile:
            calls = self.state.get("stats", 0) + 1
            self.state["stats"] = calls
            modified = MODIFIED if calls == 1 else MODIFIED + timedelta(seconds=1)
            return RemoteFile(remote_path, len(self.content), modified)

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=lambda: ChangesDuringResumeTransport(CONTENT, state),
        block_size=1024,
    ).download_files((source,), run_id=160)[0]

    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.INTEGRITY
    assert state["stats"] == 2
    assert state["offsets"] == [1024]
    assert partial.read_bytes() == b""
    assert not (tmp_path / "downloads" / "payload.bin").exists()


@pytest.mark.parametrize("layout", ("sharded", "legacy"))
def test_disk_preflight_counts_only_known_partial_remainder(
    layout: str,
    tmp_path: Path,
) -> None:
    Usage = namedtuple("Usage", "total used free")
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    part = (
        staging / part_name[:2] / part_name
        if layout == "sharded"
        else staging / part_name
    )
    part.parent.mkdir(parents=True)
    part.write_bytes(CONTENT[:-1024])
    state: dict = {}
    remaining_with_reserve = int(1024 * 1.10)

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
        disk_usage=lambda path: Usage(
            100_000,
            100_000 - remaining_with_reserve,
            remaining_with_reserve,
        ),
    ).download_files((source,), run_id=153)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == len(CONTENT) - 1024
    assert state["offsets"] == [len(CONTENT) - 1024]
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


def test_mtime_failure_preserves_complete_partial_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_metadata_update(*args, **kwargs) -> None:
        raise OSError(errno.EIO, "fallo simulado al guardar metadata")

    monkeypatch.setattr("app.downloader.os.utime", fail_metadata_update)
    state: dict = {}
    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((remote(),), run_id=154)[0]

    final = tmp_path / "downloads" / "payload.bin"
    partials = list((tmp_path / "downloads" / ".staging").rglob("*.part"))
    assert outcome.status == DownloadStatus.FAILED
    assert outcome.error_type == ErrorType.DISK_WRITE
    assert not final.exists()
    assert len(partials) == 1
    assert partials[0].read_bytes() == CONTENT


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


def test_legacy_flat_partial_is_migrated_and_resumed(tmp_path: Path) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    staging.mkdir(parents=True)
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    legacy = staging / part_name
    legacy.write_bytes(CONTENT[:1024])
    state: dict = {}

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((source,), run_id=18)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == 1024
    assert state["offsets"] == [1024]
    assert not legacy.exists()
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


def test_failed_legacy_migration_reuses_flat_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    staging.mkdir(parents=True)
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    legacy = staging / part_name
    legacy.write_bytes(CONTENT[:1024])
    sharded = staging / part_name[:2] / part_name
    real_replace = os.replace
    migrations: list[tuple[Path, Path]] = []

    def replace_with_failed_migration(source_path, destination_path) -> None:
        source_candidate = Path(source_path)
        destination_candidate = Path(destination_path)
        if source_candidate == legacy and destination_candidate == sharded:
            migrations.append((source_candidate, destination_candidate))
            raise PermissionError("migración bloqueada")
        real_replace(source_path, destination_path)

    monkeypatch.setattr("app.downloader.os.replace", replace_with_failed_migration)
    state: dict = {}
    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((source,), run_id=19)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == 1024
    assert migrations == [(legacy, sharded)]
    assert not legacy.exists()
    assert not sharded.exists()


def test_existing_shard_is_preferred_over_legacy_partial(tmp_path: Path) -> None:
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    legacy = staging / part_name
    sharded = staging / part_name[:2] / part_name
    sharded.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy incompleto")
    sharded.write_bytes(CONTENT[:1024])
    state: dict = {}

    outcome = DownloadEngine(
        connection(retries=0),
        portable_root=tmp_path,
        transport_factory=factory(state),
        block_size=1024,
    ).download_files((source,), run_id=20)[0]

    assert outcome.status == DownloadStatus.OK
    assert outcome.resumed_from == 1024
    assert legacy.read_bytes() == b"legacy incompleto"
    assert (tmp_path / "downloads" / "payload.bin").read_bytes() == CONTENT


def test_disk_preflight_does_not_migrate_legacy_partial(tmp_path: Path) -> None:
    Usage = namedtuple("Usage", "total used free")
    source = remote()
    staging = tmp_path / "downloads" / ".staging"
    staging.mkdir(parents=True)
    identity = "|".join(
        ("3", source.remote_path, MODIFIED.isoformat(), str(len(CONTENT)))
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    legacy = staging / part_name
    legacy.write_bytes(CONTENT[:1024])
    sharded = staging / part_name[:2] / part_name

    engine = DownloadEngine(
        connection(),
        portable_root=tmp_path,
        transport_factory=factory({}),
        disk_usage=lambda path: Usage(100, 100, 0),
    )
    with pytest.raises(RecolectaError) as raised:
        engine.download_files((source,), run_id=21)

    assert raised.value.error_type == ErrorType.DISK_SPACE
    assert legacy.exists()
    assert not sharded.exists()


def test_cleanup_is_recursive_bounded_and_retention_aware(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".staging"
    first_shard = staging / "aa"
    empty_shard = staging / "bb"
    nested = staging / "cc" / "deep"
    first_shard.mkdir(parents=True)
    empty_shard.mkdir()
    nested.mkdir(parents=True)
    active = first_shard / "active.part"
    old = first_shard / "old.part"
    recent = first_shard / "recent.part"
    empty = empty_shard / "empty.part"
    deep_old = nested / "deep-old.part"
    legacy_old = staging / "legacy-old.part"
    unrelated = first_shard / "keep.txt"
    active.write_bytes(b"a")
    old.write_bytes(b"bbb")
    recent.write_bytes(b"recent")
    empty.write_bytes(b"")
    deep_old.write_bytes(b"dd")
    legacy_old.write_bytes(b"llll")
    unrelated.write_bytes(b"c")
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    old_timestamp = (cutoff - timedelta(days=1)).timestamp()
    for candidate in (active, old, deep_old, legacy_old):
        os.utime(candidate, (old_timestamp, old_timestamp))

    result = cleanup_orphaned_staging(
        staging,
        active_part_names={active.name},
        cutoff=cutoff,
    )

    assert result.files_examined == 6
    assert result.files_removed == 4
    assert result.bytes_removed == 9
    assert result.errors == 0
    assert result.shards_removed == 1
    assert active.exists()
    assert recent.exists()
    assert unrelated.exists()
    assert not old.exists()
    assert not empty.exists()
    assert not deep_old.exists()
    assert not legacy_old.exists()
    assert not empty_shard.exists()


def test_cleanup_without_cutoff_keeps_nonempty_partial(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    shard = staging / "01"
    shard.mkdir(parents=True)
    nonempty = shard / "old.part"
    empty = shard / "empty.part"
    nonempty.write_bytes(b"keep")
    empty.write_bytes(b"")

    result = cleanup_orphaned_staging(staging, active_part_names=set())

    assert result.files_examined == 2
    assert result.files_removed == 1
    assert result.bytes_removed == 0
    assert nonempty.exists()
    assert not empty.exists()


def test_cleanup_reports_inaccessible_or_unsafe_root(tmp_path: Path) -> None:
    not_a_directory = tmp_path / ".staging"
    not_a_directory.write_bytes(b"file")

    result = cleanup_orphaned_staging(
        not_a_directory,
        active_part_names=set(),
        cutoff=datetime.now(timezone.utc),
    )

    assert result.errors == 1
    assert result.files_examined == 0


def test_cleanup_does_not_follow_directory_symlinks(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    outside = tmp_path / "outside"
    staging.mkdir()
    outside.mkdir()
    external_partial = outside / "external.part"
    external_partial.write_bytes(b"external")
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).timestamp()
    os.utime(external_partial, (old_timestamp, old_timestamp))
    try:
        os.symlink(outside, staging / "aa", target_is_directory=True)
    except OSError:
        pytest.skip("El entorno no permite crear symlinks de directorio.")

    result = cleanup_orphaned_staging(
        staging,
        active_part_names=set(),
        cutoff=datetime.now(timezone.utc) - timedelta(days=7),
    )

    assert result.files_examined == 0
    assert result.files_removed == 0
    assert external_partial.exists()


def test_sha256_and_outcome_are_persisted_in_run_files(tmp_path: Path) -> None:
    database = Database(tmp_path / "data" / "recolecta.db")
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
