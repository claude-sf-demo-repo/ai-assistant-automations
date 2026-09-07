"""Unit tests for `adapters/gmail/auth.py`'s network-free surface (issue #11).

`GmailToolClient` itself needs live Google credentials and isn't exercised here (see its
module docstring). `_merge_full_and_raw` -- the pure per-message combination step
`_history_list`'s two-API-call fan-out relies on -- IS network-free and IS exercised here,
against realistic per-`format` fixtures (`tests/unit/fixtures/gmail_message_resources.py`),
per the fix for the critical review finding: a real `format="raw"` response has no
`payload` key, and a real `format="full"` response has no `raw` key, so `to_envelope`
(which needs both) can only work against the merged shape this function produces.
"""

from __future__ import annotations

import pytest

from adapters.gmail.adapter import to_envelope
from adapters.gmail.auth import _merge_full_and_raw
from shared.events.payload_store import LocalPayloadStore
from tests.unit.fakes import FakeClock
from tests.unit.fixtures.gmail_message_resources import (
    FULL_MESSAGE_RESOURCE,
    RAW_MESSAGE_RESOURCE,
)

_ACCOUNT_REF = "personal"


class TestFixturesMatchRealPerFormatShapes:
    """Guards against silently drifting back into the bug the review caught: these
    fixtures must keep looking like what the real API actually returns per format.
    """

    def test_full_format_fixture_has_no_raw_field(self) -> None:
        assert "raw" not in FULL_MESSAGE_RESOURCE

    def test_full_format_fixture_has_payload_headers(self) -> None:
        assert "headers" in FULL_MESSAGE_RESOURCE["payload"]

    def test_raw_format_fixture_has_no_payload_field(self) -> None:
        assert "payload" not in RAW_MESSAGE_RESOURCE

    def test_raw_format_fixture_has_raw(self) -> None:
        assert "raw" in RAW_MESSAGE_RESOURCE


class TestMergeFullAndRaw:
    def test_merge_carries_over_payload_headers_from_full(self) -> None:
        merged = _merge_full_and_raw(FULL_MESSAGE_RESOURCE, RAW_MESSAGE_RESOURCE)
        assert merged["payload"]["headers"] == FULL_MESSAGE_RESOURCE["payload"]["headers"]

    def test_merge_carries_over_raw_from_raw_response(self) -> None:
        merged = _merge_full_and_raw(FULL_MESSAGE_RESOURCE, RAW_MESSAGE_RESOURCE)
        assert merged["raw"] == RAW_MESSAGE_RESOURCE["raw"]

    def test_merge_does_not_mutate_its_inputs(self) -> None:
        full_before = dict(FULL_MESSAGE_RESOURCE)
        raw_before = dict(RAW_MESSAGE_RESOURCE)
        _merge_full_and_raw(FULL_MESSAGE_RESOURCE, RAW_MESSAGE_RESOURCE)
        assert FULL_MESSAGE_RESOURCE == full_before
        assert RAW_MESSAGE_RESOURCE == raw_before

    def test_merged_message_has_both_payload_and_raw(self) -> None:
        merged = _merge_full_and_raw(FULL_MESSAGE_RESOURCE, RAW_MESSAGE_RESOURCE)
        assert "payload" in merged
        assert "raw" in merged


class TestMergedMessageFeedsToEnvelopeSuccessfully:
    """The actual regression check: this is the shape `GmailToolClient._history_list`
    now produces (full + raw merged), fed straight into `to_envelope` -- the exact path
    that used to raise `KeyError` on `message["payload"]` before the fix, because it was
    a bare `format="raw"` response with no `payload` key.
    """

    def test_to_envelope_succeeds_against_merged_full_and_raw(self, tmp_path) -> None:
        merged = _merge_full_and_raw(FULL_MESSAGE_RESOURCE, RAW_MESSAGE_RESOURCE)
        record = {"kind": "messageAdded", "history_id": "hist-999", "message": merged}
        clock = FakeClock()
        payload_store = LocalPayloadStore(tmp_path / "payloads")

        envelope = to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=payload_store)

        assert envelope.thread_ref == "thread-full-raw-1"
        assert envelope.sender.address == "alice@example.com"
        assert envelope.sender.domain_authenticated.spf == "pass"
        assert envelope.sender.domain_authenticated.dkim == "pass"
        assert envelope.sender.domain_authenticated.dmarc == "pass"
        assert envelope.subject.value == "Re: contract"

    def test_to_envelope_raises_keyerror_against_raw_only_response(self, tmp_path) -> None:
        """Documents the exact defect the review caught: feeding a bare `format="raw"`
        response (no `payload` key) straight to `to_envelope`, as the old
        `_history_list` did, fails.
        """
        record = {"kind": "messageAdded", "history_id": "hist-999", "message": RAW_MESSAGE_RESOURCE}
        clock = FakeClock()
        payload_store = LocalPayloadStore(tmp_path / "payloads")

        with pytest.raises(KeyError):
            to_envelope(record, _ACCOUNT_REF, clock=clock, payload_store=payload_store)
