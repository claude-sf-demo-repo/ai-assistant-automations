"""Store protocol surface the Gmail poller needs (core/ingestion, issue #11).

`shared/events/seams.py::Store` sketches the full persistence boundary (spec 02 scope,
landing in E2). This module defines the narrower slice of that boundary the E1 poller
loop depends on *now*: reading/advancing a per-`(source_system, account_ref)` cursor and
enqueueing a batch of envelopes, with both operations committing atomically inside one
`transaction()` block (spec 01 "Poller loop").

Only `Protocol`s live here -- no implementation. This task (Task 3) builds and tests
`adapters/gmail/adapter.py::poll_once` entirely against an in-memory fake
(`tests/unit/fakes.py::FakeIngestStore`) implementing `IngestStore`. Task 4 supplies the
real Postgres-backed implementation against this same protocol; a shared integration test
wires the two together in Task 4 or 5. `core/` code must never import anything from
`adapters/gmail` -- this protocol is the only contract between them.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from shared.events.envelope import CommonEventEnvelope


class IngestTransaction(Protocol):
    """The transactional surface available inside an `IngestStore.transaction()` block.

    Any `enqueue_many` and `set_cursor` calls made against the same `IngestTransaction`
    must commit together, atomically, when the `with` block exits normally. If the block
    raises, neither the queued envelopes nor the cursor advance may become visible --
    the caller must retry the whole poll from the old cursor, and redelivery is expected
    to be absorbed by `CommonEventEnvelope.idempotency_key`'s uniqueness constraint.
    """

    def enqueue_many(self, envelopes: list[CommonEventEnvelope]) -> None: ...

    def set_cursor(self, source_system: str, account_ref: str, cursor: str) -> None: ...


class IngestStore(Protocol):
    """Cursor read + transactional enqueue surface (spec 01 "Poller loop")."""

    def get_cursor(self, source_system: str, account_ref: str) -> str | None: ...

    def transaction(self) -> AbstractContextManager["IngestTransaction"]: ...
