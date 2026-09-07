"""In-memory fakes for the injectable seams (shared/events/seams.py), per spec 00's
"Test seams" section: "All four protocols have in-memory fakes in `tests/`."

`FakeClock` was added in Task 1. Task 3 (issue #11) adds `FakeGmailToolClient` (a scripted
`ToolClient`) and `FakeIngestStore` (an in-memory `core/ingestion/store.py::IngestStore`),
used to test the Gmail poller without real credentials or a real database. Fakes for
`ModelClient` and the full `shared/events/seams.py::Store` land alongside their real
implementations in later tasks/epics.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any


class FakeClock:
    """Deterministic, settable/advanceable stand-in for `Clock`.

    Defaults to a fixed instant rather than the real wall clock so tests never depend on
    when they happen to run.
    """

    def __init__(self, initial: datetime | None = None) -> None:
        self._current = initial or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def set(self, when: datetime) -> None:
        self._current = when

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class FakeGmailToolClient:
    """Scripted stand-in for the real `GmailToolClient` (adapters/gmail/auth.py).

    Returns pre-recorded `history.list` responses in call order; never touches the
    network or real OAuth credentials. Records every call made against it so tests can
    assert on the args the poller passed (e.g. `startHistoryId`).
    """

    def __init__(self, history_list_responses: list[dict[str, Any]]) -> None:
        self._responses = list(history_list_responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        if tool != "history.list":
            raise NotImplementedError(
                f"FakeGmailToolClient only scripts 'history.list', got {tool!r}"
            )
        if not self._responses:
            raise AssertionError("FakeGmailToolClient exhausted its scripted responses")
        return self._responses.pop(0)


class _FakeIngestTransaction:
    """Pending writes for one `FakeIngestStore.transaction()` block.

    Nothing here is visible on the store until `FakeIngestStore._Transaction.__exit__`
    commits it (only on normal exit -- an exception discards it entirely).
    """

    def __init__(self) -> None:
        self.pending_envelopes: list[Any] = []
        self.pending_cursors: dict[tuple[str, str], str] = {}

    def enqueue_many(self, envelopes: list[Any]) -> None:
        self.pending_envelopes.extend(envelopes)

    def set_cursor(self, source_system: str, account_ref: str, cursor: str) -> None:
        self.pending_cursors[(source_system, account_ref)] = cursor


class FakeIngestStore:
    """In-memory stand-in for `core/ingestion/store.py::IngestStore`.

    Commits are atomic: `enqueue_many` and `set_cursor` calls made inside one
    `transaction()` block only become visible (on `queue` / via `get_cursor`) if the
    `with` block exits without raising. If it raises, neither the queued envelopes nor
    the cursor advance are applied -- mirrors the real Postgres transaction semantics
    Task 4 will provide against the same `IngestStore` protocol.

    Enqueue is idempotent by `idempotency_key`, matching the real work queue's unique
    constraint (spec 01 "Work queue (Postgres)"): re-enqueuing an envelope with a key
    already present in `queue` is a no-op rather than a duplicate row.
    """

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], str] = {}
        self.queue: list[Any] = []
        self._seen_idempotency_keys: set[str] = set()

    def get_cursor(self, source_system: str, account_ref: str) -> str | None:
        return self._cursors.get((source_system, account_ref))

    def transaction(self) -> AbstractContextManager[_FakeIngestTransaction]:
        return self._Transaction(self)

    class _Transaction:
        def __init__(self, store: "FakeIngestStore") -> None:
            self._store = store
            self._tx = _FakeIngestTransaction()

        def __enter__(self) -> _FakeIngestTransaction:
            return self._tx

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if exc_type is not None:
                return False  # rollback: pending writes simply discarded
            for envelope in self._tx.pending_envelopes:
                if envelope.idempotency_key in self._store._seen_idempotency_keys:
                    continue
                self._store._seen_idempotency_keys.add(envelope.idempotency_key)
                self._store.queue.append(envelope)
            self._store._cursors.update(self._tx.pending_cursors)
            return False
