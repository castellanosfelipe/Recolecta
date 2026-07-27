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
