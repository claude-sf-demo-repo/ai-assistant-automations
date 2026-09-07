"""Unit tests for `core/ingestion/retry.py::compute_backoff` (issue #16).

Pure function, no DB, no wall clock -- deterministic given the same `attempts`, and
monotonically non-decreasing as `attempts` grows (spec 01's backoff acceptance
criterion).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core.ingestion.retry import compute_backoff


class TestComputeBackoffIsDeterministic:
    def test_same_attempts_always_yields_same_backoff(self) -> None:
        assert compute_backoff(3) == compute_backoff(3)

    @pytest.mark.parametrize("attempts", [0, 1, 2, 5, 10, 50])
    def test_returns_a_timedelta(self, attempts: int) -> None:
        assert isinstance(compute_backoff(attempts), timedelta)


class TestComputeBackoffIsMonotonicallyNonDecreasing:
    def test_backoff_never_decreases_as_attempts_increase(self) -> None:
        backoffs = [compute_backoff(n) for n in range(20)]
        for earlier, later in zip(backoffs, backoffs[1:]):
            assert later >= earlier

    def test_backoff_is_capped_rather_than_growing_unbounded(self) -> None:
        # Large attempt counts must not blow up to absurd durations -- capped growth.
        assert compute_backoff(100) == compute_backoff(1000)


class TestComputeBackoffRejectsNegativeAttempts:
    def test_negative_attempts_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_backoff(-1)
