"""Retry/backoff/poison-threshold policy for the ingest worker (issue #16).

`core/ingestion/worker.py::_handle_failure` was Task 5's explicit stub for this: a
fixed backoff plus an inline poison-threshold check, with a docstring TODO pointing
here. This module is the real policy, kept intentionally free of any Postgres/psycopg
dependency beyond the `QueueRepository`/`QueueRow` types it's handed -- `compute_backoff`
in particular has no I/O at all, so it's unit-testable with no database.

Both `attempts` (as tracked on `QueueRow`) and `poison_threshold` follow the same
convention already established by `storage/repositories/queue.py::QueueRepository.
move_to_dlq`/`mark_retry`: `row.attempts` is the count of attempts *before* the one
that just raised, so `row.attempts + 1` is the attempt that just failed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from shared.events.seams import Clock

if TYPE_CHECKING:
    from storage.repositories.queue import QueueRepository, QueueRow

__all__ = ["compute_backoff", "handle_failure"]

# Base and cap chosen to match Task 5's fixed 30s default at low attempt counts while
# still growing -- 2**1=2s, 2**2=4s, ... capped so a poison-threshold-adjacent message
# never waits absurdly long before its final DLQ-bound attempt.
_BASE_SECONDS = 2.0
_CAP_SECONDS = 300.0


def compute_backoff(attempts: int) -> timedelta:
    """Deterministic exponential backoff for the `attempts`-th failed attempt.

    `min(base * 2**attempts, cap)` -- monotonically non-decreasing in `attempts` (spec
    01's retry/backoff acceptance criterion) and a pure function of `attempts` alone:
    no `random`, no wall clock, no I/O. Jitter is deliberately omitted; if ever added it
    MUST take an explicit seeded/injectable source (e.g. a `random.Random` instance
    passed in), never module-level unseeded `random`, so this stays reproducible in
    tests.
    """
    if attempts < 0:
        raise ValueError(f"attempts must be >= 0, got {attempts}")
    seconds = min(_BASE_SECONDS * (2**attempts), _CAP_SECONDS)
    return timedelta(seconds=seconds)


def handle_failure(
    queue_repo: "QueueRepository",
    row: "QueueRow",
    exc: Exception,
    poison_threshold: int,
    clock: Clock,
) -> None:
    """Decide retry-with-backoff vs. DLQ for a queue row whose handler just raised `exc`.

    `row.attempts + 1` (the attempt that just failed, per this module's docstring
    convention) compared against `poison_threshold`: below it, retry with the backoff
    for that attempt count (spec 01 invariant 6 doesn't kick in yet); at or above it,
    move to `ingest_dlq` so the poison row never blocks the queue head.

    `clock` is accepted (rather than reading the wall clock here) to match spec 00's
    "no bare wall-clock reads outside a `Clock` seam" invariant even though the current
    policy doesn't need `clock.now()` directly -- `mark_retry`'s `not_before` is computed
    in SQL via `now() + interval`. Keeping the parameter means a future backoff variant
    that *does* need "now" (e.g. absolute jitter windows) doesn't need a signature change
    here or at the worker call site.
    """
    del clock  # not needed by the current policy; kept for the Clock-seam invariant, see docstring
    attempt_count = row.attempts + 1
    if attempt_count >= poison_threshold:
        queue_repo.move_to_dlq(row.id, f"{type(exc).__name__}: {exc}")
    else:
        backoff = compute_backoff(attempt_count)
        queue_repo.mark_retry(row.id, backoff.total_seconds())
