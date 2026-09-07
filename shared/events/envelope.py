"""Common Event Envelope (spec 00 - "Common Event Envelope").

The single shape every channel adapter normalizes to. Core imports nothing
provider-specific; email (Gmail) is the only concrete channel in v1, but the shape is
designed to admit others later.

Trust-boundary vocabulary (spec 00 / spec 07), enforced structurally here:
- Every provider-supplied string (subject, display name, ...) is wrapped in
  `UntrustedString`, whose `untrusted` flag is a `Literal[True]` — there is no way to
  construct one with `untrusted=False`. This module does not decide what is *done* with
  untrusted content (that's the mutation layer, spec 02); it only guarantees the flag
  can never be dropped or forged to `False`.
- `domain_authenticated` (computed auth results: SPF/DKIM/DMARC) and `identity_trusted`
  (read from a user-curated allowlist) are independent fields with independent
  constructors. Nothing in this module derives one from the other — see
  `test_sender_trust_axes_are_independent` in the test suite, which asserts this
  structurally (every combination of the two fields is constructible and round-trips
  unchanged) rather than just documenting it.

Validation entry point: construct via `CommonEventEnvelope(**data)` or
`CommonEventEnvelope.model_validate(data)`. Both are overridden to catch pydantic's
`ValidationError` and re-raise as `EnvelopeValidationError` — callers of this module
should only ever need to catch the one typed exception, not reach into pydantic.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

__all__ = [
    "EnvelopeValidationError",
    "UntrustedString",
    "DomainAuthResult",
    "Sender",
    "Participant",
    "Provenance",
    "CommonEventEnvelope",
]

# Deliberately permissive (no email-validator dependency required): a structural
# shape check (local@domain.tld), not a full RFC 5321 grammar. Good enough to catch
# malformed/missing addresses; not a substitute for the domain-auth results below,
# which is where actual sender legitimacy is asserted.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class EnvelopeValidationError(Exception):
    """Raised for any Common Event Envelope validation failure.

    Wraps pydantic's `ValidationError` so callers depend on one typed exception from
    this module rather than reaching into pydantic. The original pydantic error is
    preserved on `.source` (and via exception chaining, `__cause__`) for debugging.
    """

    def __init__(self, message: str, *, source: PydanticValidationError | None = None) -> None:
        super().__init__(message)
        self.source = source


class _StrictModel(BaseModel):
    """Base for all envelope models: reject unknown fields.

    Extra fields are a smuggling vector (e.g. a caller attempting to sneak in a
    same-named-but-different-semantics field); forbidding them keeps the schema the
    single source of truth for what's representable.
    """

    model_config = ConfigDict(extra="forbid")


class UntrustedString(_StrictModel):
    """A provider-supplied string, always flagged as untrusted data.

    `untrusted` is `Literal[True]` with no other allowed value: there is no
    constructor call, no dict payload, that produces an `UntrustedString` with
    `untrusted=False`. Pydantic rejects any other literal at validation time.
    """

    value: str
    untrusted: Literal[True] = True


class DomainAuthResult(_StrictModel):
    """Computed SPF/DKIM/DMARC outcomes. Never hand-set from a trust decision."""

    spf: Literal["pass", "fail", "neutral", "none", "softfail", "temperror", "permerror"]
    dkim: Literal["pass", "fail", "neutral", "none", "temperror", "permerror"]
    dmarc: Literal["pass", "fail", "none", "temperror", "permerror"]


class Sender(_StrictModel):
    """The message sender, split across two independent trust axes.

    `domain_authenticated` and `identity_trusted` are unrelated fields: one is
    computed from cryptographic auth signals, the other is a lookup against a
    user-curated allowlist. Neither this class nor any validator in this module
    derives one from the other — both are plain, independently-settable inputs. Every
    combination (authenticated-but-not-trusted, trusted-but-auth-failed, etc.) is
    representable; deciding what to *do* with a given combination is a later spec's
    business (spec 07).
    """

    address: str
    display_name: UntrustedString
    domain_authenticated: DomainAuthResult
    identity_trusted: bool
    known_contact: bool = False

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError(f"{value!r} is not a valid email address")
        return value


class Participant(_StrictModel):
    """A thread participant (spec 04 owner coreference)."""

    address: str
    role: Literal["from", "to", "cc", "bcc"]

    @field_validator("address")
    @classmethod
    def _validate_address(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError(f"{value!r} is not a valid email address")
        return value


class Provenance(_StrictModel):
    """Pointer back to the source system's own identifiers."""

    source_system_id: str
    history_id: str | None = None


class CommonEventEnvelope(_StrictModel):
    """The single shape every channel adapter normalizes to (spec 00)."""

    event_id: UUID
    idempotency_key: str
    source_system: Literal["gmail"]
    channel: Literal["email"]
    type: Literal["message.received", "message.replied", "thread.updated"]
    account_ref: str = Field(min_length=1)
    occurred_at: datetime
    ingested_at: datetime
    thread_ref: str = Field(min_length=1)
    sender: Sender
    subject: UntrustedString
    participants: list[Participant]
    payload_ref: str
    payload_sha256: str
    provenance: Provenance

    # -- Construction: translate pydantic's ValidationError into our typed error --
    #
    # pydantic v2 doesn't route `model_validate()` through `__init__`, so both entry
    # points are overridden. Nested model construction (e.g. `Sender(...)` on its own)
    # is unaffected by design -- only the top-level envelope is expected to be the
    # catch boundary callers rely on.

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except PydanticValidationError as exc:
            raise EnvelopeValidationError(str(exc), source=exc) from exc

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> "CommonEventEnvelope":
        try:
            return super().model_validate(obj, *args, **kwargs)
        except PydanticValidationError as exc:
            raise EnvelopeValidationError(str(exc), source=exc) from exc

    @field_validator("occurred_at", "ingested_at")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be tz-aware (spec implies UTC)")
        return value

    @model_validator(mode="after")
    def _check_idempotency_key(self) -> "CommonEventEnvelope":
        expected = f"{self.provenance.source_system_id}|{self.type}"
        if self.idempotency_key != expected:
            raise ValueError(
                f"idempotency_key {self.idempotency_key!r} does not match "
                f"source_system_id|type ({expected!r})"
            )
        return self
