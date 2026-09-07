"""Unit tests for the Common Event Envelope (shared/events/envelope.py, issue #12)."""

import pytest

from shared.events.envelope import (
    CommonEventEnvelope,
    DomainAuthResult,
    EnvelopeValidationError,
    Sender,
    UntrustedString,
)
from tests.unit.fixtures.envelopes import (
    MALFORMED_FIXTURES,
    WELL_FORMED_BUT_HOSTILE_CONTENT_FIXTURES,
    build_valid_envelope,
    valid_envelope_payload,
)

_AUTH_PASS = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
_AUTH_FAIL = {"spf": "fail", "dkim": "fail", "dmarc": "fail"}


def _display_name() -> dict:
    return {"value": "A. Sender", "untrusted": True}


class TestValidEnvelopeRoundTrip:
    def test_build_valid_envelope_succeeds(self) -> None:
        envelope = build_valid_envelope()
        assert isinstance(envelope, CommonEventEnvelope)
        assert envelope.account_ref == "personal"

    def test_model_dump_then_model_validate_round_trips(self) -> None:
        envelope = build_valid_envelope()
        dumped = envelope.model_dump(mode="json")
        rebuilt = CommonEventEnvelope.model_validate(dumped)
        assert rebuilt == envelope

    def test_construction_via_kwargs_also_works(self) -> None:
        payload = valid_envelope_payload()
        envelope = CommonEventEnvelope(**payload)
        assert envelope.thread_ref == "gmail-thread-id-1"


class TestMalformedFixturesRaiseTypedError:
    @pytest.mark.parametrize(
        "name,payload", MALFORMED_FIXTURES, ids=[n for n, _ in MALFORMED_FIXTURES]
    )
    def test_malformed_payload_raises_envelope_validation_error(
        self, name: str, payload: dict
    ) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(payload)

    @pytest.mark.parametrize(
        "name,payload", MALFORMED_FIXTURES, ids=[n for n, _ in MALFORMED_FIXTURES]
    )
    def test_malformed_payload_raises_envelope_validation_error_via_kwargs(
        self, name: str, payload: dict
    ) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope(**payload)

    def test_raised_error_is_not_the_raw_pydantic_error(self) -> None:
        with pytest.raises(EnvelopeValidationError) as exc_info:
            CommonEventEnvelope.model_validate(valid_envelope_payload(account_ref=""))
        # Callers should be able to catch EnvelopeValidationError alone; the raw
        # pydantic error is preserved for debugging but is not what's raised.
        assert exc_info.value.source is not None
        assert exc_info.value.__cause__ is exc_info.value.source


class TestHostileContentIsRepresentedNotRejected:
    """Correctly-wrapped hostile-looking content is data, not a schema violation
    (spec 00 "content-is-data-never-instructions"). Detection/enforcement is the
    mutation layer's job, not this schema's.
    """

    @pytest.mark.parametrize(
        "name,payload",
        WELL_FORMED_BUT_HOSTILE_CONTENT_FIXTURES,
        ids=[n for n, _ in WELL_FORMED_BUT_HOSTILE_CONTENT_FIXTURES],
    )
    def test_hostile_but_well_formed_content_does_not_raise(
        self, name: str, payload: dict
    ) -> None:
        envelope = CommonEventEnvelope.model_validate(payload)
        assert envelope.subject.untrusted is True
        assert envelope.sender.display_name.untrusted is True


class TestUntrustedStringCannotBeForgedFalse:
    def test_direct_construction_with_untrusted_false_is_rejected_by_pydantic(
        self,
    ) -> None:
        with pytest.raises(Exception):
            UntrustedString(value="x", untrusted=False)  # type: ignore[arg-type]

    def test_default_is_true(self) -> None:
        assert UntrustedString(value="hello").untrusted is True


class TestSenderTrustAxesAreIndependent:
    """Structural assertion (not just documentation) that `domain_authenticated`
    and `identity_trusted` are independent fields with independent constructors:
    every combination is constructible, and this module contains no code path that
    derives one from the other.
    """

    @pytest.mark.parametrize(
        "auth,trusted",
        [
            (_AUTH_PASS, True),
            (_AUTH_PASS, False),
            (_AUTH_FAIL, True),  # trusted allowlist hit despite auth failure
            (_AUTH_FAIL, False),
        ],
    )
    def test_every_combination_of_auth_and_trust_is_representable(
        self, auth: dict, trusted: bool
    ) -> None:
        sender = Sender(
            address="a@example.com",
            display_name=UntrustedString(value="A. Sender"),
            domain_authenticated=DomainAuthResult(**auth),
            identity_trusted=trusted,
        )
        assert sender.domain_authenticated.model_dump() == auth
        assert sender.identity_trusted is trusted

    def test_fields_are_independently_typed_with_no_derivation_link(self) -> None:
        # Sender declares domain_authenticated and identity_trusted as two entirely
        # separate model fields; there is no single input from which both are
        # derived, and no default for one that references the other's value or type.
        fields = Sender.model_fields
        assert "domain_authenticated" in fields
        assert "identity_trusted" in fields
        assert fields["identity_trusted"].annotation is bool
        assert fields["domain_authenticated"].annotation is DomainAuthResult
        # Neither field is marked required=False with a default sourced from the
        # other (both have their own, independent required/default status).
        assert fields["identity_trusted"].is_required() is True
        assert fields["domain_authenticated"].is_required() is True


class TestFieldSpecificRules:
    def test_missing_account_ref_rejected(self) -> None:
        payload = {k: v for k, v in valid_envelope_payload().items() if k != "account_ref"}
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(payload)

    def test_thread_ref_empty_string_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(valid_envelope_payload(thread_ref=""))

    def test_idempotency_key_must_match_provenance_and_type(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(
                valid_envelope_payload(idempotency_key="wrong|key")
            )

    def test_idempotency_key_matching_provenance_and_type_is_accepted(self) -> None:
        envelope = build_valid_envelope()
        assert envelope.idempotency_key == "gmail_msg_123|message.received"

    def test_naive_occurred_at_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(
                valid_envelope_payload(occurred_at="2026-09-07T09:12:00")
            )

    def test_naive_ingested_at_rejected(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            CommonEventEnvelope.model_validate(
                valid_envelope_payload(ingested_at="2026-09-07T09:14:03")
            )
