"""Single-consumer `ingest-worker` (spec 01 "Processes", issue #15).

Per spec 01, exactly one process is the deterministic writer: `ingest-worker`. It is the
only thing in the whole system that ever claims from `ingest_queue`, folds events, and
writes `thread_fold_projection` (Task 5's E1 stand-in for the real E2 state layer). The
poller (`adapters/gmail/adapter.py::poll_once`) and any n8n transport only enqueue --
they never call anything in this module.

`process_one` claims exactly one queue row, holds a per-`thread_ref`
`pg_advisory_xact_lock` for the whole time it's processing that row, folds the
accumulated event-time history for the thread, persists it, and acks the queue row --
all inside the one transaction the caller's `conn` belongs to, so claim / lock / fold /
ack either all become visible together or none do (spec 01 invariant 4: "All work for a
given `thread_ref` is serialized").

Failure handling here is a deliberate first-pass stub: `_handle_failure` inlines a fixed
backoff and Task 5's own `poison_threshold` check. Task 6 owns the real retry/backoff
policy (exponential backoff, alerting) and is expected to extract this into its own
`retry.py`, replacing `_handle_failure` -- this task's brief explicitly allows exactly
that ("Task 6 will refactor/extract retry.py").

`run_forever` is the loop wrapper: it drains `process_one` until the queue is empty, then
waits for either a Postgres `NOTIFY` wake hint on `NOTIFY_CHANNEL` or `poll_interval_
seconds` to elapse (whichever comes first), then drains again. NOTIFY delivery is not
durable (see the Task 4 migration's docstring), so the poll-on-a-timer behavior is the
safety net, not an optimization -- a worker that was down or not yet LISTENing when
`NOTIFY` fired still picks up the work on its next poll. Nothing in this loop calls the
bare wall clock or bare `time.sleep`: the wait uses `psycopg`'s own notify-with-timeout
primitive, and `sleep_fn` is an explicit seam so tests can intercept/count loop
iterations deterministically instead of racing a real timer.
"""

from __future__ import annotations

import time
from typing import Callable

import psycopg

from core.ingestion.fold import fold_thread_state
from shared.events.clock import SystemClock
from shared.events.envelope import CommonEventEnvelope
from shared.events.seams import Clock
from storage.repositories.queue import NOTIFY_CHANNEL, QueueRepository, QueueRow
from storage.repositories.thread_fold import ThreadFoldRepository

__all__ = ["process_one", "run_forever"]

DEFAULT_POISON_THRESHOLD = 5
DEFAULT_RETRY_BACKOFF_SECONDS = 30.0


def _accumulate_if_new(
    source_events: list[dict], envelope_payload: dict
) -> list[dict]:
    """Append `envelope_payload` to `source_events` unless an envelope with the same
    `idempotency_key` is already present (issue #17).

    This is what makes fold accumulation idempotent against redelivery: if a queue row
    is ever reprocessed against a thread whose fold already incorporated that exact
    envelope -- e.g. a future replay path (Task 6), or any code inserted between
    `fold_repo.upsert()` and `queue_repo.mark_done()` that causes a reprocess -- the
    envelope is not appended a second time, so `fold_thread_state` never double-counts
    it. Matching is by `idempotency_key`, the same uniqueness key `ingest_queue` itself
    enforces (spec 00: "`idempotency_key` uniquely identifies an event; re-delivery is a
    no-op"), not by list position or object identity.

    Returns a new list; never mutates `source_events` in place.
    """
    if any(e["idempotency_key"] == envelope_payload["idempotency_key"] for e in source_events):
        return list(source_events)
    return [*source_events, envelope_payload]


def _handle_failure(
    queue_repo: QueueRepository,
    row: QueueRow,
    exc: Exception,
    poison_threshold: int,
) -> None:
    """First-pass failure handling (Task 5 stub; Task 6 owns the real policy).

    `row.attempts` is the count *before* this failed attempt; `+ 1` accounts for the
    attempt that just raised `exc`. Once that reaches `poison_threshold`, the row moves
    to `ingest_dlq` (spec 01 invariant 6: "A poison message lands in the DLQ and never
    blocks the queue head") instead of being retried again.
    """
    if row.attempts + 1 >= poison_threshold:
        queue_repo.move_to_dlq(row.id, f"{type(exc).__name__}: {exc}")
    else:
        queue_repo.mark_retry(row.id, DEFAULT_RETRY_BACKOFF_SECONDS)


def process_one(
    queue_repo: QueueRepository,
    fold_repo: ThreadFoldRepository,
    conn: psycopg.Connection,
    poison_threshold: int = DEFAULT_POISON_THRESHOLD,
    clock: Clock | None = None,
) -> bool:
    """Claim and process exactly one queue row. Returns `False` if the queue was empty.

    `queue_repo` and `fold_repo` MUST be bound to `conn` -- this function commits `conn`
    itself once processing (success or handled failure) is complete, so the advisory
    lock (transaction-scoped) releases at the same instant the claim/ack/fold become
    durable. `clock` defaults to the real `SystemClock` (spec 00 invariant 5: no bare
    wall-clock reads) and is only used to stamp `thread_fold_projection.updated_at`.
    """
    clock = clock or SystemClock()
    row = queue_repo.claim_one()
    if row is None:
        conn.commit()  # release the read-only snapshot; nothing was claimed
        return False

    try:
        envelope = CommonEventEnvelope.model_validate(row.envelope)

        # Per-thread advisory lock (spec 01 "Work queue (Postgres)"): transaction-scoped
        # (`pg_advisory_xact_lock`), auto-released on this transaction's commit/rollback,
        # held for the ENTIRE remainder of this function so all mutations for this
        # thread serialize even if the queue is later parallelized.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (envelope.thread_ref,))

        existing = fold_repo.get(envelope.account_ref, envelope.thread_ref)
        source_events = _accumulate_if_new(
            list(existing.source_events) if existing else [], row.envelope
        )

        envelopes = [CommonEventEnvelope.model_validate(e) for e in source_events]
        new_state = fold_thread_state(envelopes)

        fold_repo.upsert(
            envelope.account_ref,
            envelope.thread_ref,
            source_events,
            new_state,
            updated_at=clock.now(),
        )
        queue_repo.mark_done(row.id)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any handler failure retries/DLQs
        _handle_failure(queue_repo, row, exc, poison_threshold)

    conn.commit()
    return True


def run_forever(
    dsn: str,
    *,
    poll_interval_seconds: float = 2.0,
    poison_threshold: int = DEFAULT_POISON_THRESHOLD,
    clock: Clock | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_iterations: int | None = None,
) -> None:
    """The `ingest-worker` loop (issue #15): drain the queue, wait for a wake hint or a
    poll-interval timeout, repeat.

    Opens two connections deliberately: `listen_conn` (autocommit, `LISTEN`-only) and
    `work_conn` (the one `QueueRepository`/`ThreadFoldRepository`/`process_one` share for
    claim/lock/fold/ack). Keeping them separate means the `LISTEN` session's own
    read-only snapshot never interferes with `work_conn`'s transactions.

    `max_iterations` (default `None` = forever) is a test seam: integration tests pass a
    small bound so `run_forever` returns instead of blocking the test suite.
    `sleep_fn` is invoked (with `0.0`) once per iteration that found no work and no
    `NOTIFY` within `poll_interval_seconds` -- purely so a test can substitute a stub
    that counts/interrupts iterations deterministically; the actual bounded wait already
    happened via `listen_conn.notifies(timeout=...)` before `sleep_fn` is called.
    """
    with psycopg.connect(dsn, autocommit=True) as listen_conn, psycopg.connect(dsn) as work_conn:
        with listen_conn.cursor() as cur:
            cur.execute(f"LISTEN {NOTIFY_CHANNEL}")

        queue_repo = QueueRepository(work_conn)
        fold_repo = ThreadFoldRepository(work_conn)

        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1

            while process_one(queue_repo, fold_repo, work_conn, poison_threshold, clock):
                pass

            woke_on_notify = False
            for _ in listen_conn.notifies(timeout=poll_interval_seconds):
                woke_on_notify = True
                break
            if not woke_on_notify:
                sleep_fn(0.0)
