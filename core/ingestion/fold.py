"""Pure event-time ordering / folding (spec 01 "Event-time ordering", issue #17).

`fold_thread_state` is deliberately free of I/O: no `Clock`, no `Store`, no network, no
database. It takes the full list of `CommonEventEnvelope`s contributing to one thread so
far and returns a single deterministic `ThreadFoldState` -- the same state regardless of
what order the events were *enqueued*/*delivered* in, because ordering is always
recomputed from `occurred_at` (event-time), never from arrival order (spec 00 invariant
4, spec 01 invariant 5).

Tie-break rule (documented per the brief): when two events share the exact same
`occurred_at`, the sort key falls back to `provenance.source_system_id` (a string,
compared lexicographically). This is arbitrary but *deterministic* -- what matters for
the property test isn't which of two simultaneous events "wins" positionally, it's that
every shuffle of the same input list produces the exact same tie-break decision every
time. `source_system_id` is a stable, always-present field (spec 00's envelope shape),
which is why it's the tie-break key rather than e.g. `event_id` (also stable, but
`source_system_id` is closer to the domain -- it's the provider's own message
identifier, so the resulting order is legible to a human debugging it).

This module is used by `core/ingestion/worker.py::process_one`, which is responsible for
gathering "all events contributing to a thread so far" (from `storage/repositories/
thread_fold.py`) and re-running the fold over the whole accumulated set on every new
event -- correctness, not incremental cleverness, is the point in E1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.events.envelope import CommonEventEnvelope

__all__ = ["ThreadFoldState", "fold_thread_state"]


@dataclass(frozen=True)
class ThreadFoldState:
    """The deterministic, event-time-ordered summary of one thread's events so far.

    Deliberately minimal for E1: just enough to prove event-time ordering end-to-end
    (the acceptance criteria's shuffle property test) without pre-empting Epic E2's real
    mutation/state layer, which will derive richer per-thread state from the same
    envelopes via `core/mutation.apply()`.
    """

    thread_ref: str
    account_ref: str
    last_occurred_at: datetime
    message_count: int
    last_type: str
    participant_addresses: frozenset[str]


def fold_thread_state(events: list[CommonEventEnvelope]) -> ThreadFoldState:
    """Fold a thread's events into a single deterministic `ThreadFoldState`.

    `events` must be non-empty and every envelope must share the same `(account_ref,
    thread_ref)` -- this function folds one thread at a time; scoping across threads is
    the caller's job (`ThreadFoldRepository` keys storage by that same pair).

    Ordering is by `(occurred_at, provenance.source_system_id)` -- see the module
    docstring for the tie-break rationale. This makes the sort a total order over any
    input list, including one with duplicate `occurred_at` timestamps, so the result is
    identical no matter what permutation of the same events is passed in (the property
    the acceptance criteria's shuffle test exercises directly).
    """
    if not events:
        raise ValueError("fold_thread_state requires at least one event")

    thread_refs = {e.thread_ref for e in events}
    account_refs = {e.account_ref for e in events}
    if len(thread_refs) > 1 or len(account_refs) > 1:
        raise ValueError(
            "fold_thread_state requires every event to share the same "
            f"(account_ref, thread_ref); got account_refs={account_refs!r}, "
            f"thread_refs={thread_refs!r}"
        )

    ordered = sorted(events, key=lambda e: (e.occurred_at, e.provenance.source_system_id))
    last = ordered[-1]
    participants = frozenset(
        participant.address for event in ordered for participant in event.participants
    )

    return ThreadFoldState(
        thread_ref=last.thread_ref,
        account_ref=last.account_ref,
        last_occurred_at=last.occurred_at,
        message_count=len(ordered),
        last_type=last.type,
        participant_addresses=participants,
    )
