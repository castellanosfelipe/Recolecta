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


def test_missing_remote_timestamp_uses_supplied_run_time(
    tmp_path: Path,
) -> None:
    destination = build_destination(
        connection(dest_template=r"{yyyy}\{MM}\{dd}\{filename}"),
        RemoteFile("/entrada/sin-fecha.bin", 10, None),
        portable_root=tmp_path,
        run_id=7,
        fallback_time=datetime(
            2026,
            1,
            1,
            4,
            30,
            tzinfo=timezone.utc,
        ),
    )

    assert destination.path.parts[-4:] == (
        "2025",
        "12",
        "31",
        "sin-fecha.bin",
    )


def test_remote_tree_preserves_posix_hierarchy_and_separates_siblings(
    tmp_path: Path,
) -> None:
    configured = connection(
        remote_paths=("/entrada",),
        dest_template=r"{remote_tree}",
    )

    first = build_destination(
        configured,
        remote("/entrada/a/reporte.csv"),
        portable_root=tmp_path,
        run_id=1,
    )
    second = build_destination(
        configured,
        remote("/entrada/b/reporte.csv"),
        portable_root=tmp_path,
        run_id=1,
    )

    assert first.path.parts[-3:] == ("entrada", "a", "reporte.csv")
    assert second.path.parts[-3:] == ("entrada", "b", "reporte.csv")
    assert first.path != second.path
    assert first.path.is_relative_to(first.root)
    assert second.path.is_relative_to(second.root)


def test_remote_tree_accepts_matching_unc_and_omits_server(
    tmp_path: Path,
) -> None:
    configured = connection(
        protocol=Protocol.SMB,
        host="files.example.test",
        remote_paths=(r"\\FILES.EXAMPLE.TEST\share",),
        dest_template=r"{remote_tree}",
    )

    destination = build_destination(
        configured,
        remote(r"\\files.example.test\share\a\f.bin"),
        portable_root=tmp_path,
        run_id=1,
    )

    assert destination.path.parts[-3:] == ("share", "a", "f.bin")
    assert destination.path.is_relative_to(destination.root)


def test_remote_tree_rejects_unc_from_another_host(tmp_path: Path) -> None:
    configured = connection(
        protocol=Protocol.SMB,
        host="files.example.test",
        remote_paths=(r"\\files.example.test\share",),
        dest_template=r"{remote_tree}",
    )

    with pytest.raises(RecolectaError) as raised:
        build_destination(
            configured,
            remote(r"\\other.example.test\share\a\f.bin"),
            portable_root=tmp_path,
            run_id=1,
        )

    assert raised.value.error_type == ErrorType.PATH_INVALID


def test_remote_tree_maps_local_absolute_smb_fixture_below_destination(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "share"
    configured = connection(
        protocol=Protocol.SMB,
        remote_paths=(str(fixture_root),),
        dest_template=r"{remote_tree}",
    )

    destination = build_destination(
        configured,
        remote(str(fixture_root / "nested" / "f.bin")),
        portable_root=tmp_path,
        run_id=1,
    )

    assert destination.path.parts[-3:] == ("share", "nested", "f.bin")
    assert destination.path.is_relative_to(destination.root)


def test_remote_tree_supports_multiple_and_overlapping_roots(
    tmp_path: Path,
) -> None:
    configured = connection(
        remote_paths=("/entrada", "/entrada/equipo", "/salida"),
        dest_template=r"{remote_tree}",
    )

    nested = build_destination(
        configured,
        remote("/entrada/equipo/a/f.bin"),
        portable_root=tmp_path,
        run_id=1,
    )
    second_root = build_destination(
        configured,
        remote("/salida/a/f.bin"),
        portable_root=tmp_path,
        run_id=1,
    )

    assert nested.path.parts[-4:] == ("entrada", "equipo", "a", "f.bin")
    assert second_root.path.parts[-3:] == ("salida", "a", "f.bin")
    assert nested.path != second_root.path


def test_remote_tree_uses_longest_matching_local_fixture_root(
    tmp_path: Path,
) -> None:
    parent_root = tmp_path / "share"
    nested_root = parent_root / "equipo"
    configured = connection(
        protocol=Protocol.SMB,
        remote_paths=(str(parent_root), str(nested_root)),
        dest_template=r"{remote_tree}",
    )

    destination = build_destination(
        configured,
        remote(str(nested_root / "a" / "f.bin")),
        portable_root=tmp_path,
        run_id=1,
    )

    assert destination.path.parts[-3:] == ("equipo", "a", "f.bin")
    assert destination.path.is_relative_to(destination.root)


def test_remote_tree_disambiguates_segments_changed_by_windows_sanitizing(
    tmp_path: Path,
) -> None:
    configured = connection(
        remote_paths=("/entrada",),
        dest_template=r"{remote_tree}",
    )

    colon = build_destination(
        configured,
        remote("/entrada/a:b/reporte?.csv"),
        portable_root=tmp_path,
        run_id=1,
    )
    question = build_destination(
        configured,
        remote("/entrada/a?b/reporte*.csv"),
        portable_root=tmp_path,
        run_id=1,
    )
    colon_again = build_destination(
        configured,
        remote("/entrada/a:b/reporte?.csv"),
        portable_root=tmp_path,
        run_id=99,
    )

    assert colon.path != question.path
    assert colon.path == colon_again.path
    assert colon.path.suffix == ".csv"
    assert question.path.suffix == ".csv"
    assert colon.path.is_relative_to(colon.root)
    assert question.path.is_relative_to(question.root)


@pytest.mark.parametrize(
    "malicious",
    [
        "../../../Windows/System32/evil.dll",
        r"..\..\evil.dll",
        r"C:\Windows\System32\evil.dll",
        r"\\server\share\evil.dll",
        "/entrada/./evil.dll",
        "/entrada/\x00evil.dll",
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
