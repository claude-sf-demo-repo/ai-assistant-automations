"""Unit / property tests for `core/ingestion/fold.py::fold_thread_state` (issue #17).

Pure logic -- no Postgres, no `Clock`, no I/O. The end-to-end (through the real queue and
worker) and concurrency variants of this same property live in
`tests/integration/test_ingest_worker.py`.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from core.ingestion.fold import ThreadFoldState, fold_thread_state
from tests.unit.fixtures.envelopes import build_valid_envelope

_THREAD_REF = "thread-fold-1"
_ACCOUNT_REF = "personal"
_BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _synthetic_thread_events() -> list:
    """5-8 synthetic envelopes for one thread: mixed types, occurred_at out of the
    order they'd naturally be enqueued/delivered in, distinct source_system_ids (so the
    tie-break rule is exercised deterministically, not accidentally satisfied).
    """
    specs = [
        # (minutes_offset, type, source_system_id, sender_address)
        (10, "message.received", "gmail_msg_010", "a@example.com"),
        (0, "message.received", "gmail_msg_000", "b@example.com"),
        (30, "message.replied", "gmail_msg_030", "c@example.com"),
        (5, "message.replied", "gmail_msg_005", "d@example.com"),
        (20, "message.received", "gmail_msg_020", "e@example.com"),
        (15, "message.received", "gmail_msg_015", "f@example.com"),
        (25, "message.replied", "gmail_msg_025", "g@example.com"),
    ]
    events = []
    for minutes, event_type, source_id, address in specs:
        events.append(
            build_valid_envelope(
                idempotency_key=f"{source_id}|{event_type}",
                type=event_type,
                thread_ref=_THREAD_REF,
                account_ref=_ACCOUNT_REF,
                occurred_at=(_BASE_TIME + timedelta(minutes=minutes)).isoformat(),
                provenance={"source_system_id": source_id},
                sender={"address": address},
                participants=[
                    {"address": address, "role": "from"},
                    {"address": "me@example.com", "role": "to"},
                ],
            )
        )
    return events


class TestFoldThreadStateProperty:
    """Acceptance criteria: N shuffled permutations of the same event list -> identical
    `ThreadFoldState` every time.
    """

    def test_shuffled_permutations_converge_to_identical_state(self) -> None:
        events = _synthetic_thread_events()
        expected = fold_thread_state(list(events))

        rng = random.Random(1337)
        n_shuffles = 200
        for _ in range(n_shuffles):
            shuffled = list(events)
            rng.shuffle(shuffled)
            actual = fold_thread_state(shuffled)
            assert actual == expected

    def test_result_matches_manually_computed_expected_state(self) -> None:
        events = _synthetic_thread_events()
        state = fold_thread_state(events)

        assert state == ThreadFoldState(
            thread_ref=_THREAD_REF,
            account_ref=_ACCOUNT_REF,
            last_occurred_at=_BASE_TIME + timedelta(minutes=30),
            message_count=7,
            last_type="message.replied",
            participant_addresses=frozenset(
                {
                    "a@example.com",
                    "b@example.com",
                    "c@example.com",
                    "d@example.com",
                    "e@example.com",
                    "f@example.com",
                    "g@example.com",
                    "me@example.com",
                }
            ),
        )

    def test_tie_break_on_identical_occurred_at_is_deterministic_by_source_system_id(
        self,
    ) -> None:
        same_time = _BASE_TIME
        event_a = build_valid_envelope(
            idempotency_key="gmail_msg_aaa|message.received",
            type="message.received",
            thread_ref=_THREAD_REF,
            account_ref=_ACCOUNT_REF,
            occurred_at=same_time.isoformat(),
            provenance={"source_system_id": "gmail_msg_aaa"},
        )
        event_b = build_valid_envelope(
            idempotency_key="gmail_msg_bbb|message.replied",
            type="message.replied",
            thread_ref=_THREAD_REF,
            account_ref=_ACCOUNT_REF,
            occurred_at=same_time.isoformat(),
            provenance={"source_system_id": "gmail_msg_bbb"},
        )

        # "aaa" < "bbb" lexicographically, so event_b (replied) sorts last regardless of
        # which order the two are passed in -- last_type must always be "message.replied".
        state_forward = fold_thread_state([event_a, event_b])
        state_reversed = fold_thread_state([event_b, event_a])
        assert state_forward == state_reversed
        assert state_forward.last_type == "message.replied"

    def test_rejects_empty_event_list(self) -> None:
        with pytest.raises(ValueError):
            fold_thread_state([])

    def test_rejects_mismatched_thread_ref(self) -> None:
        events = _synthetic_thread_events()
        other_thread = build_valid_envelope(
            idempotency_key="gmail_msg_other|message.received",
            thread_ref="a-different-thread",
            account_ref=_ACCOUNT_REF,
            provenance={"source_system_id": "gmail_msg_other"},
        )
        with pytest.raises(ValueError):
            fold_thread_state(events + [other_thread])
