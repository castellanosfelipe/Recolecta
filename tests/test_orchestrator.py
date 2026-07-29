from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.connection_validation import ConnectionValidationResult
from app.db import ConnectionRepository, Database
from app.models import Connection, Protocol, WindowMode
from app.orchestrator import (
    PlanStatus,
    RunCoordinator,
    TimeWindow,
    dry_run,
    plan_listing,
)
from app.transports.base import ListingResult, RemoteFile, Transport
from app.platform.secrets_fernet import FernetSecretStore


def connection(**changes) -> Connection:
    base = Connection(
        id=10,
        name="Plan",
        protocol=Protocol.SFTP,
        host="example.test",
        remote_paths=("/entrada",),
        dest_root="downloads",
        window_mode=WindowMode.ROLLING_HOURS,
        window_hours=24,
        quiet_period_s=120,
        include_globs=("*.csv", "!tmp_*"),
        exclude_globs=("secret*",),
        min_size_bytes=10,
        max_size_bytes=1000,
    )
    return replace(base, **changes).normalized()


def remote(
    name: str,
    *,
    when: datetime | None,
    size: int | None = 100,
    reliable: bool = True,
    symlink: bool = False,
) -> RemoteFile:
    return RemoteFile(
        f"/entrada/{name}",
        size,
        when,
        reliable,
        "LIST" if not reliable else "test",
        symlink,
    )


def test_planner_classifies_every_filter_and_dedupe() -> None:
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    within = started - timedelta(hours=2)
    duplicate = remote("duplicate.csv", when=within)
    listing = ListingResult(
        (
            remote("ok.csv", when=within),
            duplicate,
            remote("tmp_cache.csv", when=within),
            remote("secret.csv", when=within),
            remote("notes.txt", when=within),
            remote("tiny.csv", when=within, size=1),
            remote("large.csv", when=within, size=1001),
            remote("old.csv", when=started - timedelta(days=2)),
            remote("writing.csv", when=started - timedelta(seconds=30)),
            remote("link.csv", when=within, symlink=True),
            remote("unknown.csv", when=None),
        )
    )
    plan = plan_listing(
        connection(),
        listing,
        window=TimeWindow(started - timedelta(hours=24), started),
        started_at=started,
        successful_identities={duplicate.identity},
    )
    statuses = {item.file.name: item.status for item in plan.items}
    assert statuses == {
        "duplicate.csv": PlanStatus.DUPLICATE,
        "large.csv": PlanStatus.SIZE_FILTER,
        "link.csv": PlanStatus.SYMLINK,
        "notes.txt": PlanStatus.INCLUDE_FILTER,
        "ok.csv": PlanStatus.PLANNED,
        "old.csv": PlanStatus.OUTSIDE_WINDOW,
        "secret.csv": PlanStatus.EXCLUDE_FILTER,
        "tiny.csv": PlanStatus.SIZE_FILTER,
        "tmp_cache.csv": PlanStatus.EXCLUDE_FILTER,
        "unknown.csv": PlanStatus.TIMESTAMP_MISSING,
        "writing.csv": PlanStatus.QUIET_PERIOD,
    }
    assert plan.files_to_download == (next(item.file for item in plan.items if item.file.name == "ok.csv"),)
    assert sum(plan.counters.values()) == 11


def test_unreliable_timestamps_make_plan_partial() -> None:
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    listing = ListingResult(
        (remote("legacy.csv", when=started - timedelta(hours=2), reliable=False),),
        ("Advertencia del transporte.",),
    )
    plan = plan_listing(
        connection(),
        listing,
        window=TimeWindow(started - timedelta(days=1), started),
        started_at=started,
    )
    assert plan.is_partial
    assert len(plan.warnings) == 2
    assert "precisión temporal" in plan.warnings[1]


def test_exclusion_glob_can_match_a_directory_component() -> None:
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    archived = RemoteFile(
        "/entrada/archive/old.csv",
        100,
        started - timedelta(hours=2),
    )
    plan = plan_listing(
        connection(exclude_globs=("archive",)),
        ListingResult((archived,)),
        window=TimeWindow(started - timedelta(days=1), started),
        started_at=started,
    )
    assert plan.items[0].status == PlanStatus.EXCLUDE_FILTER


def test_overlapping_remote_roots_do_not_plan_the_same_file_twice() -> None:
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    repeated = remote("same.csv", when=started - timedelta(hours=2))
    plan = plan_listing(
        connection(),
        ListingResult((repeated, repeated)),
        window=TimeWindow(started - timedelta(days=1), started),
        started_at=started,
    )
    assert [item.status for item in plan.items] == [
        PlanStatus.PLANNED,
        PlanStatus.DUPLICATE,
    ]


class RecordingTransport(Transport):
    def __init__(self, listing: ListingResult) -> None:
        self.listing = listing
        self.connected = False
        self.closed = False
        self.list_calls = 0

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.closed = True

    def list_files(self, remote_paths, *, recursive, max_depth) -> ListingResult:
        assert self.connected
        self.list_calls += 1
        return self.listing

    def stat(self, remote_path: str) -> RemoteFile:
        raise AssertionError("dry-run no necesita stat ni descarga")

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
        raise AssertionError("dry-run no descarga contenido")


def test_dry_run_only_lists_and_closes_transport() -> None:
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    transport = RecordingTransport(
        ListingResult((remote("ok.csv", when=started - timedelta(hours=2)),))
    )
    plan = dry_run(connection(), transport, started_at=started)
    assert len(plan.files_to_download) == 1
    assert transport.list_calls == 1
    assert transport.closed


def test_dry_run_loads_successful_identities_from_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "recolecta.db")
    database.initialize()
    repository = ConnectionRepository(
        database, FernetSecretStore(Fernet.generate_key())
    )
    saved = repository.create(replace(connection(), id=None))
    started = datetime(2026, 7, 27, 2, tzinfo=timezone.utc)
    downloaded = remote("already.csv", when=started - timedelta(hours=2))
    with database.connect() as db:
        run_id = db.execute(
            """
            INSERT INTO runs(
                connection_id, trigger, window_start_utc, window_end_utc,
                started_at, status
            ) VALUES (?, 'manual', '2026-07-26', '2026-07-27',
                      '2026-07-27', 'ok')
            """,
            (saved.id,),
        ).lastrowid
        db.execute(
            """
            INSERT INTO run_files(
                run_id, connection_id, remote_path, size_bytes, mtime_utc, status
            ) VALUES (?, ?, ?, ?, ?, 'ok')
            """,
            (
                run_id,
                saved.id,
                downloaded.remote_path,
                downloaded.size_bytes,
                downloaded.mtime_utc.isoformat(timespec="seconds"),
            ),
        )
    transport = RecordingTransport(ListingResult((downloaded,)))
    plan = dry_run(saved, transport, started_at=started, database=database)
    assert plan.items[0].status == PlanStatus.DUPLICATE


def test_sftp_validation_never_persists_a_new_host_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("clave-existente", encoding="utf-8")
    seen_paths: list[Path] = []

    def validate_paths(
        draft,
        *,
        secret,
        portable_root,
        known_hosts: Path,
    ):
        seen_paths.append(known_hosts)
        assert known_hosts.read_text(encoding="utf-8") == "clave-existente"
        known_hosts.write_text("clave-nueva", encoding="utf-8")
        return ConnectionValidationResult(
            local_path=draft.dest_root,
            remote_paths=draft.remote_paths,
            remote_files_found=0,
            warnings=(),
        )

    monkeypatch.setattr(
        "app.orchestrator.validate_connection_paths",
        validate_paths,
    )
    coordinator = RunCoordinator.__new__(RunCoordinator)
    coordinator.paths = SimpleNamespace(
        root=tmp_path,
        known_hosts=known_hosts,
    )

    coordinator.validate_connection_draft(
        connection(),
        secret="credencial",
    )

    assert seen_paths[0] != known_hosts
    assert known_hosts.read_text(encoding="utf-8") == "clave-existente"
