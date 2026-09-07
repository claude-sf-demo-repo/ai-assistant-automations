"""Integration tests for storage/repositories/queue.py against a real Postgres (issues
#13, #14, part of #16).

Every test here consumes the one shared `pg_dsn` fixture (tests/integration/
pg_fixture.py) -- no test opens its own ad hoc Postgres. On this dev machine `pg_dsn`
always resolves to the local `initdb`/`pg_ctl` ephemeral-cluster backend (Docker
Desktop can't pull `postgres:16` here -- see pg_fixture.py's docstring); the same tests
are expected to run unchanged under real testcontainers once Docker access is restored.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from adapters.gmail.adapter import poll_once
from shared.events.payload_store import LocalPayloadStore
from storage.repositories.queue import PostgresIngestStore, QueueRepository
from tests.unit.fakes import FakeClock, FakeGmailToolClient
from tests.unit.fixtures.envelopes import build_valid_envelope
from tests.unit.fixtures.gmail_history_responses import HISTORY_LIST_RESPONSE_CLEAN_INBOUND

_ACCOUNT_REF = "personal"


def _count_queue_rows(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_queue")
            return cur.fetchone()[0]


class TestIdempotency:
    """Issue #13: the unique constraint on `idempotency_key`."""

    def test_enqueuing_same_envelope_twice_yields_exactly_one_row(self, pg_dsn: str) -> None:
        store = PostgresIngestStore(pg_dsn)
        envelope = build_valid_envelope()

        with store.transaction() as tx:
            tx.enqueue_many([envelope])
        with store.transaction() as tx:
            tx.enqueue_many([envelope])  # same idempotency_key, redelivered

        assert _count_queue_rows(pg_dsn) == 1

    def test_concurrent_enqueue_many_with_overlapping_keys_never_duplicates_or_errors(
        self, pg_dsn: str
    ) -> None:
        store = PostgresIngestStore(pg_dsn)
        shared_envelope = build_valid_envelope()
        unique_envelopes = [
            build_valid_envelope(
                idempotency_key=f"msg-unique-{i}|message.received",
                provenance={"source_system_id": f"msg-unique-{i}"},
            )
            for i in range(5)
        ]

        errors: list[BaseException] = []

        def _enqueue(batch: list) -> None:
            try:
                with store.transaction() as tx:
                    tx.enqueue_many(batch)
            except BaseException as exc:  # noqa: BLE001 - test assertion, not production code
                errors.append(exc)

        threads = [
            threading.Thread(target=_enqueue, args=([shared_envelope, unique_envelopes[i]],))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # 1 row for the shared/overlapping key + 5 unique rows, never a duplicate.
        assert _count_queue_rows(pg_dsn) == 6


class TestClaimOneConcurrency:
    """Issue #14: `FOR UPDATE SKIP LOCKED` claim query never double-claims."""

    def test_n_concurrent_claimers_claim_m_rows_with_no_duplicates(self, pg_dsn: str) -> None:
        store = PostgresIngestStore(pg_dsn)
        m_rows = 20
        envelopes = [
            build_valid_envelope(
                idempotency_key=f"msg-claim-{i}|message.received",
                provenance={"source_system_id": f"msg-claim-{i}"},
            )
            for i in range(m_rows)
        ]
        with store.transaction() as tx:
            tx.enqueue_many(envelopes)
        assert _count_queue_rows(pg_dsn) == m_rows

        claimed_ids: list[int] = []
        lock = threading.Lock()

        def _claim_until_empty() -> None:
            with psycopg.connect(pg_dsn) as conn:
                repo = QueueRepository(conn)
                while True:
                    row = repo.claim_one()
                    conn.commit()
                    if row is None:
                        return
                    with lock:
                        claimed_ids.append(row.id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_claim_until_empty) for _ in range(8)]
            for f in futures:
                f.result()

        assert len(claimed_ids) == m_rows
        assert len(set(claimed_ids)) == m_rows  # no duplicates in the union


class TestRestartSafety:
    """Rows inserted before a simulated "restart" are still claimable afterward."""

    def test_claim_one_finds_rows_after_new_store_and_connection(self, pg_dsn: str) -> None:
        store = PostgresIngestStore(pg_dsn)
        envelope = build_valid_envelope()
        with store.transaction() as tx:
            tx.enqueue_many([envelope])

        # Simulate a process restart: brand-new store object, brand-new connection.
        del store
        restarted_store_dsn = pg_dsn
        with psycopg.connect(restarted_store_dsn) as conn:
            repo = QueueRepository(conn)
            row = repo.claim_one()
            conn.commit()

        assert row is not None
        assert row.idempotency_key == envelope.idempotency_key
        assert row.status == "leased"


class TestQueueRepositoryLifecycle:
    """`mark_done` / `mark_retry` / `move_to_dlq` state transitions."""

    def _claim_first_row(self, conn: psycopg.Connection) -> int:
        repo = QueueRepository(conn)
        row = repo.claim_one()
        assert row is not None
        return row.id

    def test_mark_done_sets_status_done(self, pg_dsn: str) -> None:
        store = PostgresIngestStore(pg_dsn)
        with store.transaction() as tx:
            tx.enqueue_many([build_valid_envelope()])

        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            queue_id = self._claim_first_row(conn)
            repo.mark_done(queue_id)
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM ingest_queue WHERE id = %s", (queue_id,))
            assert cur.fetchone()[0] == "done"

    def test_mark_retry_reopens_row_with_backoff_and_incremented_attempts(
        self, pg_dsn: str
    ) -> None:
        store = PostgresIngestStore(pg_dsn)
        with store.transaction() as tx:
            tx.enqueue_many([build_valid_envelope()])

        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            queue_id = self._claim_first_row(conn)
            repo.mark_retry(queue_id, backoff_seconds=30)
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, attempts, not_before > now() FROM ingest_queue WHERE id = %s",
                (queue_id,),
            )
            status, attempts, not_before_in_future = cur.fetchone()
            assert status == "ready"
            assert attempts == 1
            assert not_before_in_future is True

    def test_move_to_dlq_copies_row_and_deletes_from_queue(self, pg_dsn: str) -> None:
        store = PostgresIngestStore(pg_dsn)
        envelope = build_valid_envelope()
        with store.transaction() as tx:
            tx.enqueue_many([envelope])

        with psycopg.connect(pg_dsn) as conn:
            repo = QueueRepository(conn)
            queue_id = self._claim_first_row(conn)
            repo.move_to_dlq(queue_id, "boom: exceeded max attempts")
            conn.commit()

        with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_queue WHERE id = %s", (queue_id,))
            assert cur.fetchone()[0] == 0

            cur.execute(
                "SELECT idempotency_key, error, attempts FROM ingest_dlq WHERE idempotency_key = %s",
                (envelope.idempotency_key,),
            )
            dlq_key, error, attempts = cur.fetchone()
            assert dlq_key == envelope.idempotency_key
            assert error == "boom: exceeded max attempts"
            assert attempts == 1  # 0 attempts before + 1 for the failing attempt


class TestPollOnceWiredToRealPostgres:
    """End-to-end wiring: adapters/gmail/adapter.py::poll_once against the real
    `PostgresIngestStore`, not `tests/unit/fakes.py::FakeIngestStore`. Proves the real
    implementation satisfies the exact `IngestStore` protocol Task 3 built `poll_once`
    against, with no changes to `poll_once` itself.
    """

    def test_poll_once_enqueues_and_advances_cursor_atomically(self, pg_dsn, tmp_path) -> None:
        store = PostgresIngestStore(pg_dsn)
        # Deep-copied: `_extract_history_records`/`to_envelope` never mutate their input,
        # but this module-level fixture dict is shared across test *files* in the same
        # session, and at least one other test (tests/unit/test_gmail_adapter.py) mutates
        # its own copy of this same nested dict in place -- deep-copying here means this
        # test's outcome never depends on what order pytest happens to run files in.
        gmail = FakeGmailToolClient([copy.deepcopy(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)])
        clock = FakeClock()
        payload_store = LocalPayloadStore(tmp_path / "payloads")

        assert store.get_cursor("gmail", _ACCOUNT_REF) is None

        poll_once(gmail, store, clock, _ACCOUNT_REF, payload_store=payload_store)

        assert _count_queue_rows(pg_dsn) == 1
        assert store.get_cursor("gmail", _ACCOUNT_REF) == (
            HISTORY_LIST_RESPONSE_CLEAN_INBOUND["historyId"]
        )

    def test_poll_once_is_safe_to_replay_from_same_cursor(self, pg_dsn, tmp_path) -> None:
        """Redelivering the same history response (e.g. after a crash before the cursor
        advance was visible) must not create a second queue row -- issue #13's unique
        constraint is what `poll_once`'s docstring relies on for this.
        """
        store = PostgresIngestStore(pg_dsn)
        clock = FakeClock()
        payload_store = LocalPayloadStore(tmp_path / "payloads")

        gmail_first = FakeGmailToolClient([copy.deepcopy(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)])
        poll_once(gmail_first, store, clock, _ACCOUNT_REF, payload_store=payload_store)

        # Same response redelivered (simulating a retried poll from the old cursor).
        gmail_replay = FakeGmailToolClient([copy.deepcopy(HISTORY_LIST_RESPONSE_CLEAN_INBOUND)])
        poll_once(gmail_replay, store, clock, _ACCOUNT_REF, payload_store=payload_store)

        assert _count_queue_rows(pg_dsn) == 1
