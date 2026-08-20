from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

import app.orchestrator as orchestrator_module
from app.config import AppPaths
from app.db import ConnectionRepository, Database
from app.downloader import DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES
from app.models import Connection, Protocol, VerifyMode, WindowMode
from app.orchestrator import PlanStatus, RunCoordinator
from app.platform.secrets_fernet import FernetSecretStore
from app.transports.base import RemoteFile, TransferResult, Transport


NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)


@dataclass
class MemoryRemote:
    files: tuple[RemoteFile, ...]
    payloads: dict[str, bytes]
    downloads: list[str] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    listings: list[tuple[tuple[str, ...], bool, int]] = field(
        default_factory=list
    )


class MemoryTransport(Transport):
    def __init__(self, remote: MemoryRemote) -> None:
        self.remote = remote

    def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    def iter_files(
        self,
        remote_paths: tuple[str, ...],
        *,
        recursive: bool,
        max_depth: int,
    ):
        self._reset_listing_warnings()
        self.remote.listings.append((remote_paths, recursive, max_depth))
        yield from self.remote.files

    def stat(self, remote_path: str) -> RemoteFile:
        return next(
            item
            for item in self.remote.files
            if item.remote_path == remote_path
        )

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
        del block_size, on_restart
        self.remote.downloads.append(remote_path)
        self.remote.offsets.append(offset)
        chunk = self.remote.payloads[remote_path][offset:]
        if chunk:
            on_chunk(chunk)
            target.write(chunk)
        return TransferResult(
            bytes_received=len(chunk),
            resumed_from=offset,
            resume_supported=True,
        )


def _coordinator(
    tmp_path: Path,
    monkeypatch,
    remote: MemoryRemote,
    **connection_changes,
) -> tuple[RunCoordinator, Connection, Database]:
    monkeypatch.setattr(
        orchestrator_module,
        "create_transport",
        lambda connection, secret, known_hosts: MemoryTransport(remote),
    )
    paths = AppPaths.from_root(tmp_path).ensure()
    database = Database(paths.database)
    database.initialize()
    connections = ConnectionRepository(
        database,
        FernetSecretStore(Fernet.generate_key()),
    )
    values = {
        "name": "Memoria",
        "protocol": Protocol.SFTP,
        "host": "memory.example.test",
        "remote_paths": ("/entrada",),
        "dest_root": "downloads",
        "window_mode": WindowMode.ROLLING_HOURS,
        "window_hours": 1,
        "quiet_period_s": 0,
        "verify_mode": VerifyMode.SHA256,
        "max_parallel_files": 1,
        "retries": 0,
    }
    values.update(connection_changes)
    saved = connections.create(Connection(**values))
    coordinator = RunCoordinator(
        database,
        connections,
        paths,
        now=lambda: NOW,
    )
    return coordinator, saved, database


def test_default_remote_tree_preserves_nested_sibling_paths_end_to_end(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = NOW - timedelta(minutes=30)
    first_path = "/entrada/cliente-a/reporte.csv"
    second_path = "/entrada/cliente-b/reporte.csv"
    remote = MemoryRemote(
        files=(
            RemoteFile(first_path, 7, modified),
            RemoteFile(second_path, 7, modified),
        ),
        payloads={
            first_path: b"cliente",
            second_path: b"segundo",
        },
    )
    coordinator, saved, _ = _coordinator(tmp_path, monkeypatch, remote)

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    first_local = (
        tmp_path / "downloads" / "entrada" / "cliente-a" / "reporte.csv"
    )
    second_local = (
        tmp_path / "downloads" / "entrada" / "cliente-b" / "reporte.csv"
    )
    assert execution.status == "ok"
    assert saved.dest_template == r"{remote_tree}"
    assert first_local.read_bytes() == b"cliente"
    assert second_local.read_bytes() == b"segundo"
    assert first_local != second_local
    assert remote.downloads == [first_path, second_path]


def test_invalid_remote_path_is_isolated_without_aborting_valid_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = NOW - timedelta(minutes=30)
    first_path = "/entrada/valido/primero.bin"
    invalid_path = "../../../fuera-del-origen.bin"
    second_path = "/entrada/valido/segundo.bin"
    remote = MemoryRemote(
        files=(
            RemoteFile(first_path, 3, modified),
            RemoteFile(invalid_path, 4, modified),
            RemoteFile(second_path, 3, modified),
        ),
        payloads={
            first_path: b"uno",
            invalid_path: b"malo",
            second_path: b"dos",
        },
    )
    coordinator, saved, database = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
    )

    preview = coordinator.execute_connection(
        saved.id,
        trigger="manual",
        dry_run_only=True,
    )
    execution = coordinator.execute_connection(saved.id, trigger="manual")

    assert [item.status for item in preview.plan.items] == [
        PlanStatus.PLANNED,
        PlanStatus.PATH_INVALID,
        PlanStatus.PLANNED,
    ]
    assert preview.plan.is_partial is True
    assert any(
        "ruta remota no permitida" in warning
        for warning in preview.plan.warnings
    )
    assert execution.status == "partial"
    assert execution.outcome_counts == {
        "ok": 2,
        "skipped": 0,
        "failed": 1,
        "cancelled": 0,
    }
    assert remote.downloads == [first_path, second_path]
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT remote_path, status, error_type, error_msg
            FROM run_files
            WHERE run_id = ?
            ORDER BY id
            """,
            (execution.run_id,),
        ).fetchall()
        run = connection.execute(
            """
            SELECT error_type, files_found, files_downloaded,
                   files_skipped, files_failed
            FROM runs
            WHERE id = ?
            """,
            (execution.run_id,),
        ).fetchone()
    assert [(row["remote_path"], row["status"]) for row in rows] == [
        (first_path, "ok"),
        (invalid_path, "failed"),
        (second_path, "ok"),
    ]
    assert rows[1]["error_type"] == "path_invalid"
    assert "ruta" in rows[1]["error_msg"].lower()
    assert run["error_type"] == "path_invalid"
    assert run["files_found"] == 3
    assert run["files_downloaded"] == 2
    assert run["files_skipped"] == 0
    assert run["files_failed"] == 1
    assert (
        run["files_downloaded"]
        + run["files_skipped"]
        + run["files_failed"]
        == run["files_found"]
    )


def test_full_reconciliation_repairs_missing_and_different_local_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = datetime(2020, 1, 2, 3, 4, tzinfo=timezone.utc)
    remote_path = "/entrada/historico/legacy.bin"
    payload = b"contenido-remoto"
    remote = MemoryRemote(
        files=(RemoteFile(remote_path, len(payload), modified),),
        payloads={remote_path: payload},
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
        recursive=False,
        max_depth=1,
    )
    local_path = (
        tmp_path / "downloads" / "entrada" / "historico" / "legacy.bin"
    )
    extra_path = tmp_path / "downloads" / "solo-local.txt"

    first = coordinator.execute_connection(saved.id, trigger="manual")
    persisted = coordinator.runs.get_run(first.run_id)
    assert first.status == "ok"
    assert first.plan.scan_mode == "full_local_reconciliation"
    assert first.plan.items[0].status == PlanStatus.LOCAL_MISSING
    assert local_path.read_bytes() == payload
    assert remote.downloads == [remote_path]
    assert remote.listings[-1][1] is True
    assert remote.listings[-1][2] > saved.max_depth
    assert persisted["scan_mode"] == "full_local_reconciliation"
    assert persisted["discovery_recursive"] == 1
    assert persisted["discovery_max_depth"] == remote.listings[-1][2]
    assert first.summary()["discovery_scope"] == {
        "remote_paths": ["/entrada"],
        "recursive": True,
        "max_depth": remote.listings[-1][2],
    }

    local_path.unlink()
    second = coordinator.execute_connection(saved.id, trigger="manual")
    assert second.status == "ok"
    assert second.plan.items[0].status == PlanStatus.LOCAL_MISSING
    assert local_path.read_bytes() == payload
    assert remote.downloads == [remote_path, remote_path]

    extra_path.write_bytes(b"este archivo no existe en el remoto")
    third = coordinator.execute_connection(saved.id, trigger="manual")
    assert third.status == "ok"
    assert third.plan.items[0].status == PlanStatus.LOCAL_PRESENT
    assert third.plan.files_to_download_count == 0
    assert remote.downloads == [remote_path, remote_path]
    assert extra_path.read_bytes() == b"este archivo no existe en el remoto"

    local_path.write_bytes(b"x")
    fourth = coordinator.execute_connection(saved.id, trigger="manual")
    assert fourth.status == "ok"
    assert fourth.plan.items[0].status == PlanStatus.LOCAL_DIFFERENT
    assert local_path.read_bytes() == payload
    assert remote.downloads == [remote_path, remote_path, remote_path]

    mismatched_mtime = modified.timestamp() + 10
    os.utime(local_path, (mismatched_mtime, mismatched_mtime))
    fifth = coordinator.execute_connection(saved.id, trigger="manual")
    assert fifth.status == "ok"
    assert fifth.plan.items[0].status == PlanStatus.LOCAL_DIFFERENT
    assert local_path.read_bytes() == payload
    assert abs(local_path.stat().st_mtime - modified.timestamp()) <= 2
    assert remote.downloads == [
        remote_path,
        remote_path,
        remote_path,
        remote_path,
    ]
    assert extra_path.read_bytes() == b"este archivo no existe en el remoto"


def test_full_reconciliation_repairs_when_remote_metadata_is_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    remote_path = "/entrada/sin-metadata.bin"
    payload = b"contenido-remoto-sin-metadata"
    remote = MemoryRemote(
        files=(RemoteFile(remote_path, None, None),),
        payloads={remote_path: payload},
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
    )
    disk_checks: list[int] = []
    monkeypatch.setattr(
        orchestrator_module,
        "ensure_disk_space",
        lambda destination, planned_bytes, **kwargs: disk_checks.append(
            planned_bytes
        ),
    )
    local_path = tmp_path / "downloads" / "entrada" / "sin-metadata.bin"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"contenido-local-antiguo")

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    assert execution.status == "ok"
    assert execution.plan.warnings == ()
    assert any("no informaron fecha" in item for item in execution.plan.notices)
    assert execution.plan.items[0].status == PlanStatus.LOCAL_DIFFERENT
    assert "no informó tamaño ni fecha" in execution.plan.items[0].reason
    assert local_path.read_bytes() == payload
    assert remote.downloads == [remote_path]
    assert disk_checks == [
        DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES,
        DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES,
    ]


def test_unknown_size_preflight_is_bounded_by_active_workers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    file_count = 9
    files = tuple(
        RemoteFile(f"/entrada/desconocido-{index}.bin", None, None)
        for index in range(file_count)
    )
    remote = MemoryRemote(
        files=files,
        payloads={item.remote_path: b"x" for item in files},
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
        max_parallel_files=2,
    )
    disk_checks: list[int] = []
    monkeypatch.setattr(
        orchestrator_module,
        "ensure_disk_space",
        lambda destination, planned_bytes, **kwargs: disk_checks.append(
            planned_bytes
        ),
    )

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    bounded_reserve = 2 * DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES
    assert execution.status == "ok"
    assert execution.plan.warnings == ()
    assert any("no informaron fecha" in item for item in execution.plan.notices)
    assert execution.plan.files_to_download_count == file_count
    assert len(remote.downloads) == file_count
    assert disk_checks
    assert disk_checks[0] == bounded_reserve
    assert max(disk_checks) == bounded_reserve
    assert bounded_reserve < file_count * DEFAULT_UNKNOWN_SIZE_RESERVE_BYTES


def test_full_reconciliation_preflight_discounts_reliable_partial(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = NOW - timedelta(minutes=30)
    remote_path = "/entrada/reanudable.bin"
    payload = b"contenido-reanudable"
    source = RemoteFile(remote_path, len(payload), modified)
    remote = MemoryRemote(
        files=(source,),
        payloads={remote_path: payload},
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
    )
    local_path = tmp_path / "downloads" / "entrada" / "reanudable.bin"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"x" * len(payload))
    os.utime(
        local_path,
        (modified.timestamp() + 10, modified.timestamp() + 10),
    )
    identity = "|".join(
        (
            str(saved.id),
            remote_path,
            modified.isoformat(),
            str(len(payload)),
        )
    )
    part_name = f"{uuid.uuid5(uuid.NAMESPACE_URL, identity)}.part"
    part_path = (
        tmp_path
        / "downloads"
        / ".staging"
        / part_name[:2]
        / part_name
    )
    part_path.parent.mkdir(parents=True)
    part_path.write_bytes(payload[:-1])
    disk_checks: list[int] = []
    monkeypatch.setattr(
        orchestrator_module,
        "ensure_disk_space",
        lambda destination, planned_bytes, **kwargs: disk_checks.append(
            planned_bytes
        ),
    )

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    assert execution.status == "ok"
    assert execution.plan.items[0].status == PlanStatus.LOCAL_DIFFERENT
    assert remote.offsets == [len(payload) - 1]
    assert local_path.read_bytes() == payload
    assert disk_checks == [1, 1]


def test_full_reconciliation_dry_run_mirrors_destination_collisions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = NOW - timedelta(minutes=30)
    payload = b"mismo-tamano"
    first_path = "/entrada/cliente-a/reporte.csv"
    second_path = "/entrada/cliente-b/reporte.csv"
    remote = MemoryRemote(
        files=(
            RemoteFile(first_path, len(payload), modified),
            RemoteFile(second_path, len(payload), modified),
        ),
        payloads={
            first_path: payload,
            second_path: payload,
        },
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
        dest_template="{filename}",
    )
    first_destination = tmp_path / "downloads" / "reporte.csv"
    first_destination.parent.mkdir(parents=True, exist_ok=True)
    first_destination.write_bytes(payload)
    os.utime(
        first_destination,
        (modified.timestamp(), modified.timestamp()),
    )

    execution = coordinator.execute_connection(
        saved.id,
        trigger="manual",
        dry_run_only=True,
    )

    assert execution.status == "dry_run"
    assert [item.status for item in execution.plan.items] == [
        PlanStatus.LOCAL_PRESENT,
        PlanStatus.LOCAL_MISSING,
    ]
    assert execution.plan.files_to_download_count == 1
    assert remote.downloads == []


def test_full_reconciliation_dry_run_honors_persisted_collision_order(
    monkeypatch,
    tmp_path: Path,
) -> None:
    modified = NOW - timedelta(minutes=30)
    payload = b"mismo-tamano"
    first_path = "/entrada/cliente-a/reporte.csv"
    second_path = "/entrada/cliente-b/reporte.csv"
    remote = MemoryRemote(
        files=(
            RemoteFile(first_path, len(payload), modified),
            RemoteFile(second_path, len(payload), modified),
        ),
        payloads={first_path: payload, second_path: payload},
    )
    coordinator, saved, _ = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
        full_local_reconciliation=True,
        dest_template="{filename}",
    )
    candidate = (tmp_path / "downloads" / "reporte.csv").resolve()
    mapping_scope = orchestrator_module._mapping_scope(saved, tmp_path)
    second_reserved = coordinator.runs.reserve_destination(
        connection_id=saved.id,
        mapping_scope=mapping_scope,
        remote_path=second_path,
        candidate=candidate,
    )
    first_reserved = coordinator.runs.reserve_destination(
        connection_id=saved.id,
        mapping_scope=mapping_scope,
        remote_path=first_path,
        candidate=candidate,
    )
    assert second_reserved == candidate
    assert first_reserved != candidate
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(payload)
    os.utime(candidate, (modified.timestamp(), modified.timestamp()))

    execution = coordinator.execute_connection(
        saved.id,
        trigger="manual",
        dry_run_only=True,
    )

    assert [item.status for item in execution.plan.items] == [
        PlanStatus.LOCAL_MISSING,
        PlanStatus.LOCAL_PRESENT,
    ]
    assert execution.plan.files_to_download_count == 1


def test_download_keeps_invalid_utf8_binary_payload_byte_for_byte(
    monkeypatch,
    tmp_path: Path,
) -> None:
    remote_path = "/entrada/binarios/original.dat"
    payload = b"\xff\xfe\x80\x00texto\r\n\xed\xa0\x80\x00\x01"
    remote = MemoryRemote(
        files=(
            RemoteFile(
                remote_path,
                len(payload),
                NOW - timedelta(minutes=30),
            ),
        ),
        payloads={remote_path: payload},
    )
    coordinator, saved, database = _coordinator(
        tmp_path,
        monkeypatch,
        remote,
    )

    execution = coordinator.execute_connection(saved.id, trigger="manual")

    local_path = (
        tmp_path / "downloads" / "entrada" / "binarios" / "original.dat"
    )
    expected_hash = hashlib.sha256(payload).hexdigest()
    with database.connect() as connection:
        persisted_hash = connection.execute(
            "SELECT sha256 FROM run_files WHERE run_id = ?",
            (execution.run_id,),
        ).fetchone()["sha256"]
    assert execution.status == "ok"
    assert local_path.read_bytes() == payload
    assert execution.outcomes[0].sha256 == expected_hash
    assert persisted_hash == expected_hash


def test_normal_scheduled_connection_still_deduplicates_completed_window(
    monkeypatch,
    tmp_path: Path,
) -> None:
    remote_path = "/entrada/programado.csv"
    payload = b"programado"
    remote = MemoryRemote(
        files=(
            RemoteFile(
                remote_path,
                len(payload),
                NOW - timedelta(minutes=30),
            ),
        ),
        payloads={remote_path: payload},
    )
    coordinator, saved, _ = _coordinator(tmp_path, monkeypatch, remote)

    first = coordinator.execute_connection(
        saved.id,
        trigger="schedule",
        started_at=NOW,
        window_reference_at=NOW,
    )
    repeated = coordinator.execute_connection(
        saved.id,
        trigger="schedule",
        started_at=NOW,
        window_reference_at=NOW,
    )

    assert saved.full_local_reconciliation is False
    assert first.status == "ok"
    assert repeated.status == "already_completed"
    assert repeated.run_id is None
    assert remote.downloads == [remote_path]
