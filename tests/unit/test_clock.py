from datetime import datetime, timedelta, timezone

from shared.events.clock import SystemClock
from tests.unit.fakes import FakeClock


def test_system_clock_returns_timezone_aware_utc_now() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_defaults_to_fixed_instant() -> None:
    clock = FakeClock()
    assert clock.now() == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_fake_clock_set() -> None:
    clock = FakeClock()
    target = datetime(2030, 5, 1, tzinfo=timezone.utc)
    clock.set(target)
    assert clock.now() == target


def test_fake_clock_advance() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    clock.advance(timedelta(hours=3))
    assert clock.now() == datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
