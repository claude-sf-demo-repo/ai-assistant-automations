"""Postgres-backed work queue, cursor store, and DLQ repository (issues #13, #14, #16-schema).

Two surfaces live here:

- `PostgresIngestStore` / `PostgresIngestTransaction`: the real implementation of
  `core/ingestion/store.py`'s `IngestStore` / `IngestTransaction` protocol that
  `adapters/gmail/adapter.py::poll_once` runs against. Task 3 built and tested
  `poll_once` entirely against `tests/unit/fakes.py::FakeIngestStore`; this class
  satisfies the exact same protocol so `poll_once` needs no changes to run for real.
- `QueueRepository` (+ the module-level `claim_one` convenience wrapper): the
  worker-facing surface Task 5 will drive its claim/ack/retry/DLQ loop through. Not
  part of the `IngestStore` protocol -- Task 3/4 never call it -- but it lives in this
  module because the brief requires ALL raw SQL against `ingest_queue`/`ingest_dlq` to
  go through one place.

No other module opens a connection to write to `ingest_queue` or `ingest_dlq`.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from core.ingestion.store import IngestStore, IngestTransaction  # noqa: F401 (protocol docs)
from shared.events.envelope import CommonEventEnvelope

# Postgres NOTIFY channel the enqueue path wakes workers on. See the migration
# (storage/migrations/versions/0001_ingest_queue_cursor_dlq.py) for the durability
# caveat: this is a wake-up hint only, never the sole trigger a worker relies on.
NOTIFY_CHANNEL = "ingest_queue_channel"

# The exact claim query from spec 01 / the Task 4 brief: only `ready` rows whose
# `not_before` has passed, oldest-first, skipping rows already locked by another
# claimer rather than blocking on them.
_CLAIM_SQL = (
    "SELECT * FROM ingest_queue "
    "WHERE status = 'ready' AND not_before <= now() "
    "ORDER BY id "
    "FOR UPDATE SKIP LOCKED "
    "LIMIT 1"
)

# E1 has exactly one source system. `gmail_cursor` is keyed on `account_ref` alone (see
# the migration's docstring) rather than `(source_system, account_ref)`, so this module
# enforces the narrower assumption at the boundary instead of silently ignoring the
# `source_system` argument the `IngestStore` protocol still requires (for a future
# source system, this table would need a `source_system` column and this guard would be
# deleted in favor of using it).
_SUPPORTED_SOURCE_SYSTEM = "gmail"


def _require_gmail(source_system: str) -> None:
    if source_system != _SUPPORTED_SOURCE_SYSTEM:
        raise ValueError(
            f"gmail_cursor has no source_system column; only {_SUPPORTED_SOURCE_SYSTEM!r} "
            f"is supported in E1, got {source_system!r}"
        )


@dataclass(frozen=True)
class QueueRow:
    """One `ingest_queue` row, as returned by `claim_one`."""

    id: int
    idempotency_key: str
    account_ref: str
    thread_ref: str
    envelope: dict[str, Any]
    status: str
    attempts: int
    not_before: datetime
    enqueued_at: datetime


class PostgresIngestTransaction:
    """`IngestTransaction` bound to one psycopg connection (issues #13, #14).

    Every write here happens on `self._conn`; nothing is durable until the
    `PostgresIngestStore.transaction()` context manager that created this object exits
    normally and commits. This is what gives `poll_once` (adapters/gmail/adapter.py)
    its "enqueue + cursor advance commit together, or neither does" guarantee.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def enqueue_many(self, envelopes: list[CommonEventEnvelope]) -> None:
        """Bulk insert; redelivery of an already-enqueued `idempotency_key` is a no-op.

        Uses `ON CONFLICT (idempotency_key) DO NOTHING` (issue #13's unique constraint)
        so a duplicate never becomes a duplicate row or a surfaced error -- concurrent
        callers racing on the same key both succeed, exactly one row survives.
        """
        if not envelopes:
            return
        # Local to this call: tracks whether THIS `enqueue_many` call inserted any new
        # rows, so a later call on the same (long-lived) transaction that inserts
        # nothing new doesn't fire a stale NOTIFY based on an earlier call's result.
        inserted_any = False
        with self._conn.cursor() as cur:
            for envelope in envelopes:
                cur.execute(
                    """
                    INSERT INTO ingest_queue (idempotency_key, account_ref, thread_ref, envelope)
                    VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        envelope.idempotency_key,
                        envelope.account_ref,
                        envelope.thread_ref,
                        envelope.model_dump_json(),
                    ),
                )
                if cur.rowcount:
                    inserted_any = True
        if inserted_any:
            # NOTIFY payload is deliberately empty: it's a wake-up hint, not a message
            # bus (see the migration docstring on why the worker must also poll).
            with self._conn.cursor() as cur:
                cur.execute(f"NOTIFY {NOTIFY_CHANNEL}")

    def set_cursor(self, source_system: str, account_ref: str, cursor: str) -> None:
        """Upsert the Gmail history cursor for `account_ref`."""
        _require_gmail(source_system)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gmail_cursor (account_ref, history_id, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (account_ref)
                DO UPDATE SET history_id = EXCLUDED.history_id, updated_at = now()
                """,
                (account_ref, cursor),
            )


class PostgresIngestStore:
    """Real `IngestStore` implementation (issues #13, #14) that `poll_once` runs against.

    Each `transaction()` call opens its own connection so concurrent pollers (multiple
    accounts, retried polls) never share transaction state. `get_cursor` uses its own
    short-lived connection too -- it's a plain read, not part of any write transaction.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def get_cursor(self, source_system: str, account_ref: str) -> str | None:
        _require_gmail(source_system)
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT history_id FROM gmail_cursor WHERE account_ref = %s",
                    (account_ref,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[PostgresIngestTransaction]:
        """Yield a transaction-bound `IngestTransaction`; commit on clean exit, else rollback.

        `psycopg.Connection`'s own context-manager semantics provide exactly the
        atomicity `IngestTransaction`'s docstring requires: no exception -> commit,
        exception -> rollback (and re-raise) -- both close the connection either way.
        """
        with psycopg.connect(self._dsn) as conn:
            yield PostgresIngestTransaction(conn)


class QueueRepository:
    """Worker-facing surface over `ingest_queue`/`ingest_dlq` (issues #13, #14, #16-schema).

    Bound to a single connection; the CALLER owns the transaction boundary (commit or
    rollback), on purpose: Task 5's worker loop needs `claim_one`'s row lock and the
    eventual `mark_done`/`mark_retry`/`move_to_dlq` for that same row to commit
    together. If the process dies after `claim_one` but before the caller commits, the
    connection close rolls the lease back to `status='ready'` automatically -- nothing
    is only in-memory (restart-safety, per the brief's verification list).
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def claim_one(self) -> QueueRow | None:
        """Claim and lease the oldest claimable row, or `None` if there isn't one.

        `FOR UPDATE SKIP LOCKED` (issue #14) means concurrent claimers never block on
        each other and never double-claim: a row already locked by another claimer's
        open transaction is simply skipped, not waited on.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_CLAIM_SQL)
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute("UPDATE ingest_queue SET status = 'leased' WHERE id = %s", (row["id"],))
            row["status"] = "leased"
            return QueueRow(**row)

    def mark_done(self, queue_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute("UPDATE ingest_queue SET status = 'done' WHERE id = %s", (queue_id,))

    def mark_retry(self, queue_id: int, backoff_seconds: float) -> None:
        """Release the lease back to `ready`, incrementing `attempts` and pushing `not_before`
        out by `backoff_seconds`. Full retry/backoff policy (which backoff to compute, when to
        give up and call `move_to_dlq` instead) is Task 6 scope -- this method only performs the
        state transition once the caller has decided to retry.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_queue
                SET status = 'ready',
                    attempts = attempts + 1,
                    not_before = now() + %s * interval '1 second'
                WHERE id = %s
                """,
                (backoff_seconds, queue_id),
            )

    def move_to_dlq(self, queue_id: int, error: str) -> None:
        """Copy the row into `ingest_dlq`, then delete it from `ingest_queue`.

        Deleting (rather than leaving a `status='dead'` row behind) is the brief's
        preferred choice: `ingest_queue` is a working queue, not meant to accumulate
        dead rows forever, and `ingest_dlq` is the durable record of what died and why
        -- callers needing to inspect/replay dead letters (Task 6) read `ingest_dlq`,
        never `ingest_queue`. `attempts + 1` counts the attempt that caused this death,
        matching `mark_retry`'s convention of incrementing on the attempt that just ran.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingest_dlq (idempotency_key, envelope, error, attempts)
                SELECT idempotency_key, envelope, %s, attempts + 1
                FROM ingest_queue
                WHERE id = %s
                """,
                (error, queue_id),
            )
            cur.execute("DELETE FROM ingest_queue WHERE id = %s", (queue_id,))


def claim_one(conn: psycopg.Connection) -> QueueRow | None:
    """Free-function form of `QueueRepository.claim_one`, matching the brief's literal
    `claim_one(conn) -> QueueRow | None` signature for callers that don't want to hold
    onto a `QueueRepository` instance.
    """
    return QueueRepository(conn).claim_one()
