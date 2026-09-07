"""Reusable Common Event Envelope fixtures (shared/events/envelope.py).

For Task 2 (issue #12) and reuse by later tasks (e.g. Task 3's Gmail mapping tests).
This module builds fixtures via plain `dict` payloads fed through
`CommonEventEnvelope.model_validate(...)`/`CommonEventEnvelope(**...)` -- not by
pre-constructing nested pydantic models -- so that structural violations (a dropped
`untrusted` flag, a forged `untrusted: False`, a missing field) are caught by the
envelope's own validation, exactly as they would be for a real adapter payload coming
off the wire.

This is deliberately NOT the Gmail-specific `to_envelope` mapping -- that's Task 3.
These are hand-built, provider-agnostic envelope payloads.
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import uuid4

from shared.events.envelope import CommonEventEnvelope

_SOURCE_SYSTEM_ID = "gmail_msg_123"
_TYPE = "message.received"


def _base_payload() -> dict[str, Any]:
    """A fresh, valid envelope payload as a plain dict (deep-copy per call)."""
    return {
        "event_id": str(uuid4()),
        "idempotency_key": f"{_SOURCE_SYSTEM_ID}|{_TYPE}",
        "source_system": "gmail",
        "channel": "email",
        "type": _TYPE,
        "account_ref": "personal",
        "occurred_at": "2026-09-07T09:12:00+00:00",
        "ingested_at": "2026-09-07T09:14:03+00:00",
        "thread_ref": "gmail-thread-id-1",
        "sender": {
            "address": "a@example.com",
            "display_name": {"value": "A. Sender", "untrusted": True},
            "domain_authenticated": {"spf": "pass", "dkim": "pass", "dmarc": "pass"},
            "identity_trusted": False,
            "known_contact": False,
        },
        "subject": {"value": "Re: invoice", "untrusted": True},
        "participants": [
            {"address": "a@example.com", "role": "from"},
            {"address": "me@example.com", "role": "to"},
        ],
        "payload_ref": "store://raw/deadbeef",
        "payload_sha256": "deadbeef" * 8,
        "provenance": {"source_system_id": _SOURCE_SYSTEM_ID, "history_id": "hist-1"},
    }


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def valid_envelope_payload(**overrides: Any) -> dict[str, Any]:
    """A valid envelope payload (dict), with dict-valued overrides deep-merged in.

    Use `overrides={"sender": {...}}`-style nested dicts to tweak a sub-field without
    rebuilding the whole payload.
    """
    return _deep_merge(_base_payload(), overrides)


def build_valid_envelope(**overrides: Any) -> CommonEventEnvelope:
    """A valid, fully-constructed `CommonEventEnvelope`, for reuse by later tasks."""
    return CommonEventEnvelope.model_validate(valid_envelope_payload(**overrides))


# --- Malformed / injection fixtures ---------------------------------------------
#
# Each entry is (name, payload). Every one of these is expected to raise
# `EnvelopeValidationError` when fed through `CommonEventEnvelope.model_validate`.
#
# Note on the SSRF-ish / control-bidi entries: the *content itself* (a URL-like
# string, control/bidi characters) is not what's rejected -- per spec 00, provider
# content is data, never instructions, and is representable as long as it's correctly
# wrapped as `{"value": ..., "untrusted": true}`. What's rejected here is a
# *structural* violation delivered alongside that content: either the wrapper is
# missing (a bare string where an `UntrustedString` object is required) or the
# `untrusted` flag is forged to `False`. See `WELL_FORMED_BUT_HOSTILE_CONTENT_FIXTURES`
# below for the companion case -- the same hostile content, correctly wrapped, which
# must NOT raise.

MALFORMED_FIXTURES: list[tuple[str, dict[str, Any]]] = [
    (
        "missing_account_ref",
        {k: v for k, v in _base_payload().items() if k != "account_ref"},
    ),
    (
        "empty_account_ref",
        valid_envelope_payload(account_ref=""),
    ),
    (
        "empty_thread_ref",
        valid_envelope_payload(thread_ref=""),
    ),
    (
        "missing_thread_ref",
        {k: v for k, v in _base_payload().items() if k != "thread_ref"},
    ),
    (
        "idempotency_key_disagrees_with_provenance_and_type",
        valid_envelope_payload(idempotency_key="not|the-right-key"),
    ),
    (
        "occurred_at_naive_datetime",
        valid_envelope_payload(occurred_at="2026-09-07T09:12:00"),
    ),
    (
        "ingested_at_naive_datetime",
        valid_envelope_payload(ingested_at="2026-09-07T09:14:03"),
    ),
    (
        "sender_display_name_untrusted_forged_false",
        valid_envelope_payload(
            sender={"display_name": {"value": "A. Sender", "untrusted": False}}
        ),
    ),
    (
        "subject_untrusted_forged_false",
        valid_envelope_payload(subject={"value": "Re: invoice", "untrusted": False}),
    ),
    (
        "subject_missing_wrapper_structurally",
        # A bare string instead of the required {"value": ..., "untrusted": true}
        # shape -- an adapter that forgot to wrap provider content at all.
        valid_envelope_payload(subject="http://169.254.169.254/latest/meta-data/"),
    ),
    (
        "sender_display_name_missing_wrapper_structurally",
        valid_envelope_payload(sender={"display_name": "‮evil‬"}),
    ),
    (
        "sender_address_malformed",
        valid_envelope_payload(sender={"address": "not-an-email"}),
    ),
    (
        "participant_address_malformed",
        valid_envelope_payload(
            participants=[{"address": "not-an-email", "role": "from"}]
        ),
    ),
    (
        "extra_unknown_field_on_envelope",
        valid_envelope_payload(unexpected_field="smuggled"),
    ),
]


# --- Companion fixtures: hostile CONTENT, correctly wrapped -----------------------
#
# These must NOT raise. They demonstrate that this layer represents hostile-looking
# content faithfully (as flagged, untrusted data) rather than trying to detect and
# reject it -- detection/enforcement is the mutation layer's job (spec 02), not this
# schema's.

WELL_FORMED_BUT_HOSTILE_CONTENT_FIXTURES: list[tuple[str, dict[str, Any]]] = [
    (
        "ssrf_ish_subject_correctly_wrapped",
        valid_envelope_payload(
            subject={
                "value": "http://169.254.169.254/latest/meta-data/",
                "untrusted": True,
            }
        ),
    ),
    (
        "control_bidi_display_name_correctly_wrapped",
        valid_envelope_payload(
            sender={
                "display_name": {
                    "value": "‮evil‬ A. Sender",
                    "untrusted": True,
                }
            }
        ),
    ),
]
