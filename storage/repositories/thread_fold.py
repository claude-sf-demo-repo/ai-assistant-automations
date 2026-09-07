"""`thread_fold_projection`: an E1-only stand-in for the E2 mutation/state layer (issue #17).

This table is **NOT** the fact/edge store (spec 02, Epic E2). It exists so E1 can prove
event-time ordering end-to-end -- through the real Postgres work queue and a real
per-thread advisory lock (`core/ingestion/worker.py`) -- without first implementing the
mutation layer. Epic E2 will fold events through `core/mutation.apply()` instead; this
projection is expected to be superseded/removed then. Nothing here is named "fact" or
"state" in the domain sense used by spec 02 -- `ThreadFoldRepository` and
`ThreadFoldProjectionRow` are deliberately scoped names that don't collide with that
later vocabulary.

Design: rather than trying to fold incrementally (accumulate a running `ThreadFoldState`
and merge in just the new envelope), this repository persists the **full list of raw
envelope payloads** contributing to a thread so far, alongside the derived
`ThreadFoldState` recomputed from that whole list on every write. This keeps
`core/ingestion/fold.py::fold_thread_state` the single source of truth for ordering
logic -- there is no separate "incremental fold" code path to keep in sync with it, and
correctness (not performance) is what E1's acceptance criteria are actually testing.
Cost: `state` grows with thread length. Acceptable for E1's synthetic, short-lived test
threads; a real incremental fold (or migrating to E2's mutation layer entirely) is exactly
the kind of thing this table is documented above as being superseded by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.ingestion.fold import ThreadFoldState

__all__ = ["ThreadFoldProjectionRow", "ThreadFoldRepository"]


@dataclass(frozen=True)
class ThreadFoldProjectionRow:
    """One `thread_fold_projection` row, deserialized."""

    account_ref: str
    thread_ref: str
    source_events: list[dict[str, Any]]
    state: ThreadFoldState
    updated_at: datetime


def _serialize_state(state: ThreadFoldState) -> dict[str, Any]:
    return {
        "thread_ref": state.thread_ref,
        "account_ref": state.account_ref,
        "last_occurred_at": state.last_occurred_at.isoformat(),
        "message_count": state.message_count,
        "last_type": state.last_type,
        "participant_addresses": sorted(state.participant_addresses),
    }


def _deserialize_state(payload: dict[str, Any]) -> ThreadFoldState:
    return ThreadFoldState(
        thread_ref=payload["thread_ref"],
        account_ref=payload["account_ref"],
        last_occurred_at=datetime.fromisoformat(payload["last_occurred_at"]),
        message_count=payload["message_count"],
        last_type=payload["last_type"],
        participant_addresses=frozenset(payload["participant_addresses"]),
    )


class ThreadFoldRepository:
    """Persistence for `thread_fold_projection`, keyed by `(account_ref, thread_ref)`.

    Bound to a single connection; like `storage/repositories/queue.py::QueueRepository`,
    the CALLER owns the transaction boundary. `core/ingestion/worker.py::process_one`
    shares one connection/transaction across `QueueRepository.claim_one`, the advisory
    lock, this repository's `get`/`upsert`, and `QueueRepository.mark_done`, so a crash
    mid-processing rolls all of it back together -- no half-applied fold, no orphaned
    lease.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get(self, account_ref: str, thread_ref: str) -> ThreadFoldProjectionRow | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT state, updated_at FROM thread_fold_projection "
                "WHERE account_ref = %s AND thread_ref = %s",
                (account_ref, thread_ref),
            )
            row = cur.fetchone()
            if row is None:
                return None
            state_payload, updated_at = row
            return ThreadFoldProjectionRow(
                account_ref=account_ref,
                thread_ref=thread_ref,
                source_events=state_payload["source_events"],
                state=_deserialize_state(state_payload["state"]),
                updated_at=updated_at,
            )

    def upsert(
        self,
        account_ref: str,
        thread_ref: str,
        source_events: list[dict[str, Any]],
        state: ThreadFoldState,
        updated_at: datetime,
    ) -> None:
        """Replace the row for `(account_ref, thread_ref)` with the given accumulated
        `source_events` and freshly-folded `state`. `updated_at` is supplied by the
        caller (via `Clock`, spec 00 invariant 5) -- this repository never reads the
        wall clock itself.
        """
        payload = {"source_events": source_events, "state": _serialize_state(state)}
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO thread_fold_projection (account_ref, thread_ref, state, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (account_ref, thread_ref)
                DO UPDATE SET state = EXCLUDED.state, updated_at = EXCLUDED.updated_at
                """,
                (account_ref, thread_ref, Jsonb(payload), updated_at),
            )
