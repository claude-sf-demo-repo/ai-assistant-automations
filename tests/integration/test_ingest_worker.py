"""Integration tests for `core/ingestion/worker.py` (issue #15) and
`storage/repositories/thread_fold.py` (issue #17) against a real Postgres.

Every test consumes the shared `pg_dsn` fixture (tests/integration/pg_fixture.py) --
same pattern as `tests/integration/test_queue_repository.py`. On this dev machine that
always resolves to the local `initdb`/`pg_ctl` ephemeral-cluster backend (Docker can't
pull `postgres:16` here); these tests are expected to run unchanged once real
testcontainers access is available.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from core.ingestion.fold import fold_thread_state
from core.ingestion.worker import process_one, run_forever
from storage.repositories.queue import PostgresIngestStore, QueueRepository
from storage.repositories.thread_fold import ThreadFoldRepository
from tests.unit.fakes import FakeClock
from tests.unit.fixtures.envelopes import build_valid_envelope

_ACCOUNT_REF = "personal"
_BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _thread_events(thread_ref: str, tag: str, n: int = 6) -> list:
    """`n` synthetic envelopes for one thread, occurred_at deliberately out of the
    order they're listed in (and will be enqueued in), mixed message types.
    """
    events = []
    minute_offsets = [10, 0, 25, 5, 20, 15, 30, 35][:n]
    for i, minutes in enumerate(minute_offsets):
        event_type = "message.replied" if i % 3 == 2 else "message.received"
        source_id = f"gmail_msg_{tag}_{i:03d}"
        events.append(
            build_valid_envelope(
                idempotency_key=f"{source_id}|{event_type}",
                type=event_type,
                thread_ref=thread_ref,
                account_ref=_ACCOUNT_REF,
                occurred_at=(_BASE_TIME + timedelta(minutes=minutes)).isoformat(),
                provenance={"source_system_id": source_id},
                sender={"address": f"sender-{tag}-{i}@example.com"},
                participants=[
                    {"address": f"sender-{tag}-{i}@example.com", "role": "from"},
                    {"address": "me@example.com", "role": "to"},
                ],
            )
        )
    return events


def _enqueue(dsn: str, envelopes: list) -> None:
    store = PostgresIngestStore(dsn)
    with store.transaction() as tx:
        tx.enqueue_many(envelopes)


class TestEndToEndFoldViaWorkerLoop:
    """Enqueue shuffled events for one thread through the real queue; run the worker
    loop to drain it; assert the persisted `thread_fold_projection` row matches the
    in-order-computed expected state.
    """

    def test_worker_loop_drains_queue_and_persists_expected_fold_state(self, pg_dsn: str) -> None:
        thread_ref = "thread-e2e-1"
        events = _thread_events(thread_ref, "e2e")
        # Enqueue in a deliberately shuffled (non-occurred_at) order.
        shuffled = [events[3], events[0], events[5], events[1], events[4], events[2]]
        _enqueue(pg_dsn, shuffled)

        run_forever(
            pg_dsn,
            poll_interval_seconds=0.05,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )

        expected_state = fold_thread_state(events)

        with psycopg.connect(pg_dsn) as conn:
            row = ThreadFoldRepository(conn).get(_ACCOUNT_REF, thread_ref)

        assert row is not None
        assert row.state == expected_state
        assert len(row.source_events) == len(events)


class TestFoldAccumulationIsIdempotentAgainstRedelivery:
    """Code-review fix round (issue #17): a redelivered `ingest_queue` row for an
    envelope already incorporated into `thread_fold_projection` must not double-count
    that envelope in the fold. Regression test for `core/ingestion/worker.py::
    _accumulate_if_new`.

    A queue row moving back to `status = 'ready'` after having already been
    `mark_done`'d is exactly what a future replay bug (or Task 6 replay path, if it ever
    replayed an already-succeeded row) would produce -- so this test drives that
    scenario directly against the real table rather than only unit-testing the helper.
    """

    def test_reprocessing_the_same_envelope_does_not_double_count_it(self, pg_dsn: str) -> None:
        thread_ref = "thread-redelivery-1"
        events = _thread_events(thread_ref, "redeliver", n=3)
        _enqueue(pg_dsn, events)

        run_forever(
            pg_dsn,
            poll_interval_seconds=0.05,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )

        expected_state = fold_thread_state(events)
        with psycopg.connect(pg_dsn) as conn:
            row_after_first_drain = ThreadFoldRepository(conn).get(_ACCOUNT_REF, thread_ref)
        assert row_after_first_drain is not None
        assert row_after_first_drain.state == expected_state
        assert len(row_after_first_drain.source_events) == 3

        # Simulate redelivery: move one already-`done` row back to `ready` so
        # `claim_one` picks it up again, without touching its envelope content.
        with psycopg.connect(pg_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingest_queue SET status = 'ready' "
                    "WHERE idempotency_key = %s",
                    (events[0].idempotency_key,),
                )
            conn.commit()

        run_forever(
            pg_dsn,
            poll_interval_seconds=0.05,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
        )

        with psycopg.connect(pg_dsn) as conn:
            row_after_redelivery = ThreadFoldRepository(conn).get(_ACCOUNT_REF, thread_ref)

        assert row_after_redelivery is not None
        # Still exactly 3 source events and the same folded state -- the redelivered
        # envelope was recognized as already-present and not appended a second time.
        assert len(row_after_redelivery.source_events) == 3
        assert row_after_redelivery.state == expected_state


class TestConcurrency:
    """Two threads' worth of events interleaved in the queue; 2+ concurrent worker
    iterations against the same Postgres -> each thread's own events still fold
    correctly, no lost updates / no double-invalidation artifacts.
    """

    def test_concurrent_workers_fold_each_thread_correctly_with_no_lost_updates(
        self, pg_dsn: str
    ) -> None:
        thread_a = "thread-concurrent-a"
        thread_b = "thread-concurrent-b"
        events_a = _thread_events(thread_a, "ca")
        events_b = _thread_events(thread_b, "cb")

        # Interleave enqueue order across the two threads.
        interleaved = []
        for ea, eb in zip(events_a, events_b):
            interleaved.append(ea)
            interleaved.append(eb)
        _enqueue(pg_dsn, interleaved)

        errors: list[BaseException] = []

        def _drain_worker() -> None:
            try:
                with psycopg.connect(pg_dsn) as conn:
                    queue_repo = QueueRepository(conn)
                    fold_repo = ThreadFoldRepository(conn)
                    while process_one(queue_repo, fold_repo, conn, poison_threshold=5):
                        pass
            except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` for the test
                errors.append(exc)

        threads = [threading.Thread(target=_drain_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        assert not any(t.is_alive() for t in threads)

        expected_a = fold_thread_state(events_a)
        expected_b = fold_thread_state(events_b)

        with psycopg.connect(pg_dsn) as conn:
            fold_repo = ThreadFoldRepository(conn)
            row_a = fold_repo.get(_ACCOUNT_REF, thread_a)
            row_b = fold_repo.get(_ACCOUNT_REF, thread_b)

        assert row_a is not None and row_a.state == expected_a
        assert row_b is not None and row_b.state == expected_b
        assert len(row_a.source_events) == len(events_a)
        assert len(row_b.source_events) == len(events_b)


class TestAdvisoryLockActuallyBlocks:
    """A test that holds the per-thread advisory lock externally and asserts a
    concurrent `process_one` call blocks/waits rather than proceeding concurrently.
    """

    def test_process_one_blocks_while_external_connection_holds_the_thread_lock(
        self, pg_dsn: str
    ) -> None:
        thread_ref = "thread-lock-block"
        envelope = build_valid_envelope(
            idempotency_key="gmail_msg_lockblock|message.received",
            thread_ref=thread_ref,
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_lockblock"},
        )
        _enqueue(pg_dsn, [envelope])

        # Hold the same advisory key externally, in an open (uncommitted) transaction.
        holder_conn = psycopg.connect(pg_dsn)
        with holder_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (thread_ref,))
        # holder_conn deliberately NOT committed/rolled back yet -- lock stays held.

        result: dict[str, bool] = {}

        def _run_process_one() -> None:
            with psycopg.connect(pg_dsn) as work_conn:
                queue_repo = QueueRepository(work_conn)
                fold_repo = ThreadFoldRepository(work_conn)
                processed = process_one(queue_repo, fold_repo, work_conn, poison_threshold=5)
                result["processed"] = processed

        worker_thread = threading.Thread(target=_run_process_one)
        worker_thread.start()

        # Give process_one every opportunity to (incorrectly) proceed if the lock
        # weren't actually blocking it.
        time.sleep(1.0)
        assert worker_thread.is_alive(), (
            "process_one returned while the advisory lock was still held externally -- "
            "the per-thread lock is not actually blocking concurrent processing"
        )

        # Release the external lock; process_one should now be able to proceed.
        holder_conn.rollback()
        holder_conn.close()

        worker_thread.join(timeout=10)
        assert not worker_thread.is_alive(), "process_one never unblocked after the external lock was released"
        assert result.get("processed") is True

        with psycopg.connect(pg_dsn) as conn:
            row = ThreadFoldRepository(conn).get(_ACCOUNT_REF, thread_ref)
        assert row is not None
        assert row.state.message_count == 1


class TestRunForeverLoopWrapper:
    """Basic behavioral check of the loop wrapper itself: bounded iterations, no bare
    `time.sleep` (the injected `sleep_fn` is what gets called), NOTIFY-or-timeout wait.
    """

    def test_run_forever_respects_max_iterations_and_uses_injected_sleep(self, pg_dsn: str) -> None:
        sleep_calls: list[float] = []

        run_forever(
            pg_dsn,
            poll_interval_seconds=0.05,
            sleep_fn=sleep_calls.append,
            max_iterations=3,
        )

        # No work was enqueued, so every iteration should time out waiting for NOTIFY
        # and fall through to the injected sleep -- proving the wait path is exercised
        # and no bare time.sleep call is made in its place.
        assert len(sleep_calls) == 3
        assert all(call == 0.0 for call in sleep_calls)
