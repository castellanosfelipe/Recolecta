from collections import namedtuple
from io import BytesIO
from pathlib import Path

import pytest

from app.errors import ErrorType, HarvesterError
from app.integrity import StreamingVerifier, ensure_disk_space
from app.models import VerifyMode


def test_sha256_is_seeded_from_partial_and_continues_streaming() -> None:
    verifier = StreamingVerifier(VerifyMode.SHA256)
    partial = BytesIO(b"abc")
    verifier.seed_from_partial(partial, length=3, block_size=2)
    verifier.update(b"def")
    assert verifier.bytes_seen == 6
    assert (
        verifier.sha256
        == "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721"
    )


def test_size_mode_does_not_read_existing_partial() -> None:
    class NoRead(BytesIO):
        def read(self, size=-1):
            raise AssertionError("verify_mode=size no debe releer el parcial")

    verifier = StreamingVerifier(VerifyMode.SIZE)
    partial = NoRead(b"abc")
    verifier.seed_from_partial(partial, length=3)
    assert verifier.bytes_seen == 3
    assert partial.tell() == 3


def test_size_mismatch_is_integrity_error() -> None:
    verifier = StreamingVerifier(VerifyMode.SIZE)
    with pytest.raises(HarvesterError) as raised:
        verifier.verify_size(actual=9, expected=10)
    assert raised.value.error_type == ErrorType.INTEGRITY


def test_disk_preflight_requires_ten_percent_reserve(tmp_path: Path) -> None:
    Usage = namedtuple("Usage", "total used free")

    with pytest.raises(HarvesterError) as raised:
        ensure_disk_space(
            tmp_path / "not-created",
            1000,
            reserve_ratio=0.10,
            disk_usage=lambda path: Usage(10_000, 9_000, 1099),
        )
    assert raised.value.error_type == ErrorType.DISK_SPACE

    ensure_disk_space(
        tmp_path,
        1000,
        reserve_ratio=0.10,
        disk_usage=lambda path: Usage(10_000, 8_900, 1100),
    )
