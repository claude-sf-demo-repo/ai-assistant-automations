"""Ingest work queue, Gmail cursor, and dead-letter-queue tables (issues #13, #14, #16-schema)

Revision ID: 0001
Revises:
Create Date: 2026-09-07

Schema is exactly spec 01's ("Work queue (Postgres)") shape, verbatim from the E1 plan's
Task 4 brief:

- `ingest_queue`: the work queue. `idempotency_key` carries a UNIQUE constraint (issue
  #13) so `enqueue_many`'s `ON CONFLICT (idempotency_key) DO NOTHING` (storage/
  repositories/queue.py) makes redelivery a no-op instead of a duplicate row or a
  surfaced error. The partial index `ix_ingest_queue_claimable` only covers `status =
  'ready'` rows ordered by `(not_before, id)` -- exactly the predicate/order the claim
  query (issue #14) filters and sorts by, so a claim never has to scan `done`/`leased`/
  `dead` rows.
- `gmail_cursor`: one row per Gmail account (`account_ref` is the whole key -- E1 has
  exactly one source system, so there's no separate `source_system` column; see
  storage/repositories/queue.py's `_require_gmail` guard for how the `IngestStore`
  protocol's `source_system` parameter is reconciled with this single-source-system
  schema).
- `ingest_dlq`: dead letters. Deliberately no FK back to `ingest_queue` -- by the time a
  row lands here the `ingest_queue` row is being deleted in the same transaction (see
  `QueueRepository.move_to_dlq`'s docstring for why deletion, not a `status='dead'`
  row, is preferred).

NOTIFY/LISTEN (issue #14): no trigger is created here. `PostgresIngestTransaction.
enqueue_many` (storage/repositories/queue.py) issues `NOTIFY ingest_queue_channel`
itself, in the same transaction as the insert, once per `enqueue_many` call that
inserted at least one new row. Doing it in application code rather than a DB trigger
keeps the SQL surface here plain DDL (no procedural PL/pgSQL to test/maintain) and
keeps the notify's "did we actually insert anything" condition next to the code that
already computed it. IMPORTANT (documented per the brief): Postgres NOTIFY payloads and
deliveries are NOT durable -- a NOTIFY sent while no session is LISTENing (e.g. the
worker is down or between polls) is simply dropped, not queued. `NOTIFY` here is only a
wake-up hint for an already-running worker; Task 5's worker MUST also poll
`ingest_queue` on a timer as a safety net, or work enqueued while it wasn't listening
(e.g. during a restart) would never be picked up until the next unrelated NOTIFY.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE ingest_queue (
          id              bigserial PRIMARY KEY,
          idempotency_key text NOT NULL UNIQUE,
          account_ref     text NOT NULL,
          thread_ref      text NOT NULL,
          envelope        jsonb NOT NULL,
          status          text NOT NULL DEFAULT 'ready',   -- ready|leased|done|dead
          attempts        int  NOT NULL DEFAULT 0,
          not_before      timestamptz NOT NULL DEFAULT now(),
          enqueued_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_ingest_queue_claimable ON ingest_queue (not_before, id) "
        "WHERE status = 'ready'"
    )

    op.execute(
        """
        CREATE TABLE gmail_cursor (
          account_ref text PRIMARY KEY,
          history_id  text NOT NULL,
          updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE ingest_dlq (
          id bigserial PRIMARY KEY,
          idempotency_key text NOT NULL,
          envelope jsonb NOT NULL,
          error text NOT NULL,
          attempts int NOT NULL,
          died_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    Forward-only migration policy (spec 09): destructive downgrades are never implemented.
    Rolling back a bad migration means writing and applying a new forward migration that
    fixes it, not reversing history. This keeps the audit trail (and any data already
    written under the new schema) intact.
    """
    raise NotImplementedError(
        "Forward-only migrations (spec 09): write a new forward migration instead of downgrading."
    )
