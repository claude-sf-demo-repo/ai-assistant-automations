"""Unit test for `core/ingestion/worker.py::_accumulate_if_new` (issue #17 fix round).

Pure logic, no Postgres: covers the idempotent-accumulation fix directly. The
integration-level regression (a redelivered `ingest_queue` row must not double-count in
`thread_fold_projection`) lives in
`tests/integration/test_ingest_worker.py::TestFoldAccumulationIsIdempotentAgainstRedelivery`.
"""

from __future__ import annotations

from core.ingestion.worker import _accumulate_if_new


def test_appends_a_genuinely_new_envelope() -> None:
    existing = [{"idempotency_key": "a|message.received", "x": 1}]
    new = {"idempotency_key": "b|message.received", "x": 2}

    result = _accumulate_if_new(existing, new)

    assert result == [existing[0], new]
    # never mutates the input list in place
    assert existing == [{"idempotency_key": "a|message.received", "x": 1}]


def test_redelivery_of_an_already_folded_envelope_is_a_no_op() -> None:
    envelope = {"idempotency_key": "a|message.received", "x": 1}
    existing = [envelope]

    # Same idempotency_key, even if the dict is a distinct (but equal-content) object --
    # simulates a redelivered queue row whose envelope was already incorporated.
    redelivered = {"idempotency_key": "a|message.received", "x": 1}

    result = _accumulate_if_new(existing, redelivered)

    assert result == [envelope]
    assert len(result) == 1


def test_starts_from_empty_list() -> None:
    new = {"idempotency_key": "a|message.received"}
    assert _accumulate_if_new([], new) == [new]
