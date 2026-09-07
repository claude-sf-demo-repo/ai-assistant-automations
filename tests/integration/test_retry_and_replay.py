"""Integration tests for `core/ingestion/retry.py` (poison threshold -> DLQ) and
`core/ingestion/replay.py` (issue #16), against a real Postgres.

Every test consumes the shared `pg_dsn` fixture (tests/integration/pg_fixture.py) --
same pattern as `tests/integration/test_ingest_worker.py` and `test_queue_repository.py`.
"""

from __future__ import annotations

import psycopg
import pytest

from core.ingestion.fold import fold_thread_state
from core.ingestion.replay import replay
from core.ingestion.worker import process_one
from storage.repositories.queue import PostgresIngestStore, QueueRepository
from storage.repositories.thread_fold import ThreadFoldRepository
from tests.unit.fakes import FakeClock
from tests.unit.fixtures.envelopes import build_valid_envelope

_ACCOUNT_REF = "personal"
_POISON_THRESHOLD = 3


class _AlwaysRaisingFoldRepo:
    """Stand-in `ThreadFoldRepository` whose `.get` raises on every call, forcing
    `process_one` down its failure path every attempt -- the "handler forced to raise
    on every attempt" scenario the brief asks for.
    """

    def get(self, account_ref: str, thread_ref: str):  # noqa: ANN001 - test double
        raise RuntimeError("boom: handler always fails")


def _enqueue(dsn: str, envelopes: list) -> None:
    store = PostgresIngestStore(dsn)
    with store.transaction() as tx:
        tx.enqueue_many(envelopes)


class TestPoisonThresholdMovesToDlqAndQueueContinues:
    def test_poison_row_lands_in_dlq_after_threshold_and_other_rows_still_process(
        self, pg_dsn: str
    ) -> None:
        poison_envelope = build_valid_envelope(
            idempotency_key="gmail_msg_poison|message.received",
            thread_ref="thread-poison",
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_poison"},
        )
        good_envelope = build_valid_envelope(
            idempotency_key="gmail_msg_good|message.received",
            thread_ref="thread-good",
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_good"},
        )
        _enqueue(pg_dsn, [poison_envelope, good_envelope])

        clock = FakeClock()
        failing_fold_repo = _AlwaysRaisingFoldRepo()

        # Drive the poison row through `poison_threshold` failed attempts. Each
        # `process_one` call claims the oldest claimable row; between attempts the
        # poison row is the only one whose `not_before` has already passed (mark_retry
        # pushes it into the future), so re-claiming it deterministically requires
        # advancing past its backoff.
        for attempt in range(_POISON_THRESHOLD):
            with psycopg.connect(pg_dsn) as conn:
                queue_repo = QueueRepository(conn)
                processed = process_one(
                    queue_repo,
                    failing_fold_repo,
                    conn,
                    poison_threshold=_POISON_THRESHOLD,
                    clock=clock,
                )
                assert processed is True

            with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingest_queue SET not_before = now() "
                    "WHERE idempotency_key = %s",
                    (poison_envelope.idempotency_key,),
                )
                conn.commit()

        # After `poison_threshold` failed attempts, the poison row is gone from
        # `ingest_queue` and present in `ingest_dlq` with the right error/attempts.
        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_queue WHERE idempotency_key = %s",
                (poison_envelope.idempotency_key,),
            )
            assert cur.fetchone()[0] == 0

            cur.execute(
                "SELECT error, attempts FROM ingest_dlq WHERE idempotency_key = %s",
                (poison_envelope.idempotency_key,),
            )
            row = cur.fetchone()
            assert row is not None
            error, attempts = row
            assert "boom: handler always fails" in error
            assert attempts == _POISON_THRESHOLD

        # The queue continues: the good row is still claimable/claimed via the
        # `WHERE status='ready'` index -- process it for real, with a fold repo that
        # actually succeeds, and confirm it completes.
        with psycopg.connect(pg_dsn) as conn:
            queue_repo = QueueRepository(conn)
            fold_repo = ThreadFoldRepository(conn)
            processed = process_one(queue_repo, fold_repo, conn, poison_threshold=5, clock=clock)
            assert processed is True

        with psycopg.connect(pg_dsn) as conn:
            fold_row = ThreadFoldRepository(conn).get(_ACCOUNT_REF, "thread-good")
        assert fold_row is not None
        assert fold_row.state == fold_thread_state([good_envelope])


class TestReplayIsIdempotent:
    def test_replaying_a_dlq_row_twice_processes_it_exactly_once(self, pg_dsn: str) -> None:
        envelope = build_valid_envelope(
            idempotency_key="gmail_msg_replay|message.received",
            thread_ref="thread-replay",
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_replay"},
        )
        _enqueue(pg_dsn, [envelope])

        # Move it straight to the DLQ (simulating it having exhausted retries).
        with psycopg.connect(pg_dsn) as conn:
            queue_repo = QueueRepository(conn)
            row = queue_repo.claim_one()
            assert row is not None
            queue_repo.move_to_dlq(row.id, "simulated poison")
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM ingest_dlq WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            dlq_id = cur.fetchone()[0]

        # Replay once.
        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            replay(repo, repo, dlq_id)
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, attempts FROM ingest_queue WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            status, attempts = cur.fetchone()
            assert status == "ready"
            assert attempts == 0

            cur.execute("SELECT count(*) FROM ingest_dlq WHERE id = %s", (dlq_id,))
            assert cur.fetchone()[0] == 0

        # Replay again with the SAME dlq_id (row no longer exists) -- must be a no-op,
        # not an error, and must not create a second queue row.
        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            replay(repo, repo, dlq_id)
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_queue WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            assert cur.fetchone()[0] == 1

        # Drain the queue for real; the replayed row processes exactly once.
        with psycopg.connect(pg_dsn) as conn:
            queue_repo = QueueRepository(conn)
            fold_repo = ThreadFoldRepository(conn)
            while process_one(queue_repo, fold_repo, conn, poison_threshold=5):
                pass

        with psycopg.connect(pg_dsn) as conn:
            fold_row = ThreadFoldRepository(conn).get(_ACCOUNT_REF, "thread-replay")
        assert fold_row is not None
        assert len(fold_row.source_events) == 1
        assert fold_row.state == fold_thread_state([envelope])

    def test_replaying_when_idempotency_key_already_present_in_queue_is_a_no_op(
        self, pg_dsn: str
    ) -> None:
        """Guards the case in the brief where the DLQ row's `idempotency_key` is
        "somehow already present" as a ready/leased/done row in `ingest_queue` -- e.g. a
        second poll re-enqueued it while the DLQ copy still existed.
        """
        envelope = build_valid_envelope(
            idempotency_key="gmail_msg_replay2|message.received",
            thread_ref="thread-replay2",
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_replay2"},
        )
        _enqueue(pg_dsn, [envelope])

        with psycopg.connect(pg_dsn) as conn:
            queue_repo = QueueRepository(conn)
            row = queue_repo.claim_one()
            assert row is not None
            queue_repo.move_to_dlq(row.id, "simulated poison")
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM ingest_dlq WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            dlq_id = cur.fetchone()[0]

        # Re-enqueue the same envelope directly (simulating redelivery landing back on
        # the queue while the DLQ copy is still sitting there).
        _enqueue(pg_dsn, [envelope])

        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            replay(repo, repo, dlq_id)
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ingest_queue WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            assert cur.fetchone()[0] == 1  # still exactly one, not two

            # The DLQ row is still deleted by `replay` even though the insert was a
            # conflict no-op -- replay's job is "make sure it's on the queue", and it is.
            cur.execute("SELECT count(*) FROM ingest_dlq WHERE id = %s", (dlq_id,))
            assert cur.fetchone()[0] == 0
