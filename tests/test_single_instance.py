import sys
import uuid
from pathlib import Path

from app.platform.single_instance import SingleInstance


def test_single_instance_allows_only_one_holder(tmp_path: Path) -> None:
    name = rf"Local\Recolecta.Tests.{uuid.uuid4()}"
    first = SingleInstance(tmp_path, mutex_name=name)
    second = SingleInstance(tmp_path, mutex_name=name)
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    guard = SingleInstance(
        tmp_path,
        mutex_name=rf"Local\Recolecta.Tests.{uuid.uuid4()}",
    )
    assert guard.try_acquire()
    guard.release()
    guard.release()
    assert not guard.acquired
