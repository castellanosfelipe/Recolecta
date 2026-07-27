from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.errors import ErrorType, RecolectaError
from app.models import ConflictMode, Connection, Protocol
from app.naming import (
    MAX_WINDOWS_PATH,
    build_destination,
    resolve_conflict,
    sanitize_windows_segment,
)
from app.transports.base import RemoteFile


def connection(**changes) -> Connection:
    base = Connection(
        name="SFTP Producción",
        client="Cliente A",
        protocol=Protocol.SFTP,
        host="example.test",
        dest_root="downloads",
    )
    return replace(base, **changes).normalized()


def remote(path: str) -> RemoteFile:
    return RemoteFile(
        path,
        10,
        datetime(2026, 7, 26, 3, 4, 5, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("reporte:final?.csv", "reporte_final_.csv"),
        ("CON.txt", "_CON.txt"),
        ("LPT9", "_LPT9"),
        ("nombre. ", "nombre"),
    ],
)
def test_windows_segment_sanitization(source: str, expected: str) -> None:
    assert sanitize_windows_segment(source) == expected


def test_destination_template_expands_inside_root(tmp_path: Path) -> None:
    destination = build_destination(
        connection(
            dest_template=r"{client}\{connection}\{yyyy}\{MM}\{dd}\{filename}"
        ),
        remote("/entrada/reporte:final?.csv"),
        portable_root=tmp_path,
        run_id=7,
    )
    assert destination.root == (tmp_path / "downloads").resolve()
    assert destination.path.is_relative_to(destination.root)
    assert destination.path.name == "reporte_final_.csv"
    assert destination.path.parts[-6:-1] == (
        "Cliente A",
        "SFTP Producción",
        "2026",
        "07",
        "25",
    )


@pytest.mark.parametrize(
    "malicious",
    [
        "../../../Windows/System32/evil.dll",
        r"..\..\evil.dll",
        r"C:\Windows\System32\evil.dll",
        r"\\server\share\evil.dll",
    ],
)
def test_malicious_remote_paths_are_rejected(
    tmp_path: Path, malicious: str
) -> None:
    with pytest.raises(RecolectaError) as raised:
        build_destination(
            connection(),
            remote(malicious),
            portable_root=tmp_path,
            run_id=1,
        )
    assert raised.value.error_type == ErrorType.PATH_INVALID


def test_template_cannot_escape_destination(tmp_path: Path) -> None:
    with pytest.raises(RecolectaError) as raised:
        build_destination(
            connection(dest_template=r"..\outside\{filename}"),
            remote("/entrada/a.csv"),
            portable_root=tmp_path,
            run_id=1,
        )
    assert raised.value.error_type == ErrorType.PATH_INVALID


def test_long_path_is_truncated_preserving_extension(tmp_path: Path) -> None:
    destination = build_destination(
        connection(dest_template="{filename}"),
        remote("/entrada/" + "a" * 300 + ".csv"),
        portable_root=tmp_path,
        run_id=1,
    )
    assert destination.was_truncated
    assert len(str(destination.path)) <= MAX_WINDOWS_PATH
    assert destination.path.suffix == ".csv"


def test_conflict_modes(tmp_path: Path) -> None:
    existing = tmp_path / "report.csv"
    existing.write_text("old", encoding="utf-8")
    now = datetime(2026, 7, 27, 2, 3, 4, tzinfo=timezone.utc)
    assert resolve_conflict(existing, ConflictMode.SKIP, timestamp=now) is None
    assert (
        resolve_conflict(existing, ConflictMode.OVERWRITE, timestamp=now)
        == existing
    )
    kept = resolve_conflict(existing, ConflictMode.KEEP_BOTH, timestamp=now)
    assert kept is not None
    assert kept.name == "report__20260727020304.csv"
