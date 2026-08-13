import threading

from app.throttle import ThrottleManager, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def test_token_bucket_waits_for_refill_without_real_sleep() -> None:
    clock = FakeClock()
    bucket = TokenBucket(
        1024,
        capacity_bytes=1024,
        clock=clock,
        sleeper=clock.sleep,
    )
    bucket.consume(1024)
    bucket.consume(512)
    assert clock.sleeps == [0.5]


def test_token_bucket_handles_chunks_larger_than_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(
        100,
        capacity_bytes=100,
        clock=clock,
        sleeper=clock.sleep,
    )
    bucket.consume(250)
    assert sum(clock.sleeps) == 1.5


def test_host_spacing_is_injected_and_deterministic() -> None:
    clock = FakeClock()
    manager = ThrottleManager(
        global_parallelism=1,
        clock=clock,
        sleeper=clock.sleep,
    )
    with manager.transfer_slot("EXAMPLE.test", minimum_spacing_s=2):
        pass
    with manager.transfer_slot("example.TEST", minimum_spacing_s=2):
        pass
    assert clock.sleeps == [2]


def test_token_bucket_wait_is_cancel_aware() -> None:
    bucket = TokenBucket(1, capacity_bytes=1)
    assert bucket.consume(1) is True
    cancel = threading.Event()
    cancel.set()

    assert bucket.consume(1, cancel_event=cancel) is False


def test_transfer_slot_does_not_block_after_cancellation() -> None:
    manager = ThrottleManager(global_parallelism=1)
    cancel = threading.Event()
    cancel.set()

    with manager.transfer_slot(
        "example.test",
        cancel_event=cancel,
    ) as acquired:
        assert acquired is False


def test_global_bandwidth_bucket_is_shared_across_connections() -> None:
    clock = FakeClock()
    manager = ThrottleManager(
        global_parallelism=2,
        global_bandwidth_limit_kbps=1,
        clock=clock,
        sleeper=clock.sleep,
    )

    first = manager.bandwidth_buckets("connection-1", None)
    second = manager.bandwidth_buckets("connection-2", None)

    assert len(first) == len(second) == 1
    assert first[0] is second[0]
    assert first[0].consume(1024) is True
    assert second[0].consume(1024) is True
    assert clock.sleeps == [1.0]


def test_connection_and_global_bandwidth_caps_are_both_applied() -> None:
    manager = ThrottleManager(
        global_parallelism=1,
        global_bandwidth_limit_kbps=2,
    )

    first = manager.bandwidth_buckets("connection-1", 1)
    second = manager.bandwidth_buckets("connection-2", 1)

    assert len(first) == len(second) == 2
    assert first[0] is not second[0]
    assert first[1] is second[1]


def test_connection_bandwidth_bucket_tracks_an_updated_limit() -> None:
    manager = ThrottleManager(global_parallelism=1)

    original = manager.bandwidth_buckets("connection-1", 1)[0]
    unchanged = manager.bandwidth_buckets("connection-1", 1)[0]
    updated = manager.bandwidth_buckets("connection-1", 3)[0]

    assert unchanged is original
    assert updated is not original
    assert updated.rate == 3 * 1024


def test_global_bandwidth_limit_can_be_reconfigured_safely() -> None:
    manager = ThrottleManager(global_parallelism=1)
    assert manager.bandwidth_buckets("connection", None) == ()

    manager.set_global_bandwidth_limit(3)
    bucket = manager.bandwidth_buckets("connection", None)[0]
    assert bucket.rate == 3 * 1024

    manager.set_global_bandwidth_limit(None)
    assert manager.bandwidth_buckets("connection", None) == ()
