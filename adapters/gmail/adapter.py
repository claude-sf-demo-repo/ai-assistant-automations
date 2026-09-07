"""Gmail adapter: poll `history.list`, map records to `CommonEventEnvelope`s (issue #11).

Implements the exact transactional pattern from spec 01's "Poller loop": read the cursor,
call `history.list`, map every changed message to an envelope, then commit the enqueue and
the cursor advance in ONE `IngestStore.transaction()` block. If the process dies between
`enqueue_many` and `set_cursor`, the transaction rolls back; the next poll retries from the
old cursor and redelivered events are absorbed by `CommonEventEnvelope.idempotency_key`'s
uniqueness.

`core/` never imports anything from here (verified by
`tests/unit/test_gmail_adapter.py::TestCoreImportsNothingGmailSpecific`) -- this
module is the only place that knows about Gmail's wire shapes.

Trust-boundary rules this module enforces (spec 00 / spec 07), non-negotiably:
- `identity_trusted` is set to `False` unconditionally on every envelope this adapter
  produces. The allowlist lookup that would set it otherwise is a later epic (spec 07);
  this adapter must never compute or guess trust.
- `domain_authenticated` is computed ONLY by parsing the message's `Authentication-Results`
  header (SPF/DKIM/DMARC verdicts) -- never from any other signal, and never conflated
  with `identity_trusted`.
- `thread_ref` is Gmail's own `threadId` -- never the `References`/`In-Reply-To` headers,
  which are provider-supplied and spoofable.
- Every string taken from headers (subject, sender/participant display names) is wrapped
  in `UntrustedString`.

Note on `to_envelope`'s signature: spec 01 sketches it as `to_envelope(record) ->
envelope`. This implementation takes two additional required keyword-only seams --
`clock` (for `ingested_at`, which is "when we ingested this," not anything present in the
Gmail record) and `payload_store` (where the raw message bytes get written) -- rather than
reading a wall clock or a global store directly. Both are seams per shared/events/seams.py's
"no I/O outside a seam" rule.

Note on message hydration: real `history.list` calls don't inline full message bodies.
`GmailToolClient` (adapters/gmail/auth.py) is responsible for returning fully-hydrated
message resources (headers + `raw`) from its `history.list` tool call -- whatever fan-out
to `users.messages.get` that requires is that client's concern, not this module's. Tests
here script the post-fan-out shape directly (see
`tests/unit/fixtures/gmail_history_responses.py`).
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr
from typing import Any, Literal
from uuid import uuid4

from core.ingestion.store import IngestStore
from shared.events.envelope import (
    CommonEventEnvelope,
    DomainAuthResult,
    Participant,
    Provenance,
    Sender,
    UntrustedString,
)
from shared.events.payload_store import LocalPayloadStore
from shared.events.seams import Clock, ToolClient

_SOURCE_SYSTEM: Literal["gmail"] = "gmail"
_CHANNEL: Literal["email"] = "email"

_AUTH_RESULT_RE = re.compile(r"\b(spf|dkim|dmarc)=([a-zA-Z]+)", re.IGNORECASE)

_ALLOWED_SPF = {"pass", "fail", "neutral", "none", "softfail", "temperror", "permerror"}
_ALLOWED_DKIM = {"pass", "fail", "neutral", "none", "temperror", "permerror"}
_ALLOWED_DMARC = {"pass", "fail", "none", "temperror", "permerror"}


def _headers_map(headers: list[dict[str, str]]) -> dict[str, str]:
    """Case-insensitive header lookup, keyed by lowercased header name."""
    return {h["name"].lower(): h["value"] for h in headers}


def _parse_authentication_results(raw: str) -> DomainAuthResult:
    """Parse SPF/DKIM/DMARC verdicts out of an `Authentication-Results` header value.

    This is the ONLY signal `domain_authenticated` is ever computed from. A missing
    verdict (header absent, or a mechanism not mentioned) maps to `"none"`, matching the
    RFC 7601 convention that "none" means "no relevant record/verdict found" -- it is
    never treated as a pass.
    """
    verdicts = {"spf": "none", "dkim": "none", "dmarc": "none"}
    for mechanism, verdict in _AUTH_RESULT_RE.findall(raw or ""):
        mechanism = mechanism.lower()
        verdict = verdict.lower()
        if mechanism == "spf" and verdict in _ALLOWED_SPF:
            verdicts["spf"] = verdict
        elif mechanism == "dkim" and verdict in _ALLOWED_DKIM:
            verdicts["dkim"] = verdict
        elif mechanism == "dmarc" and verdict in _ALLOWED_DMARC:
            verdicts["dmarc"] = verdict
    return DomainAuthResult(**verdicts)


def _decode_raw_message(raw_b64url: str) -> bytes:
    """Decode Gmail's base64url `raw` message field (padding-tolerant)."""
    padding = "=" * (-len(raw_b64url) % 4)
    return base64.urlsafe_b64decode(raw_b64url + padding)


def _participants_from_headers(headers: dict[str, str]) -> list[Participant]:
    participants: list[Participant] = []
    for role in ("from", "to", "cc", "bcc"):
        value = headers.get(role)
        if not value:
            continue
        for _display_name, address in getaddresses([value]):
            if not address:
                continue
            participants.append(Participant(address=address, role=role))  # type: ignore[arg-type]
    return participants


def _envelope_type(
    kind: Literal["messageAdded", "messageChanged"], message: dict[str, Any]
) -> Literal["message.received", "message.replied", "thread.updated"]:
    if kind == "messageChanged":
        return "thread.updated"
    if "SENT" in message.get("labelIds", []):
        return "message.replied"
    return "message.received"


def to_envelope(
    history_record: dict[str, Any],
    account_ref: str,
    *,
    clock: Clock,
    payload_store: LocalPayloadStore,
) -> CommonEventEnvelope:
    """Map one Gmail `history.list` record to a `CommonEventEnvelope`.

    `history_record` shape: `{"kind": "messageAdded" | "messageChanged", "history_id":
    <str>, "message": <full Gmail Message resource>}` -- see
    `_extract_history_records` for how `poll_once` builds these from a raw
    `history.list` response.
    """
    kind = history_record["kind"]
    message = history_record["message"]
    headers = _headers_map(message["payload"]["headers"])

    envelope_type = _envelope_type(kind, message)

    from_display_name, from_address = parseaddr(headers.get("from", ""))
    domain_auth = _parse_authentication_results(headers.get("authentication-results", ""))

    raw_bytes = _decode_raw_message(message["raw"])
    payload_ref, payload_sha256 = payload_store.put(raw_bytes)

    occurred_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)

    source_system_id = message["id"]

    return CommonEventEnvelope(
        event_id=uuid4(),
        idempotency_key=f"{source_system_id}|{envelope_type}",
        source_system=_SOURCE_SYSTEM,
        channel=_CHANNEL,
        type=envelope_type,
        account_ref=account_ref,
        occurred_at=occurred_at,
        ingested_at=clock.now(),
        thread_ref=message["threadId"],  # Gmail's own threadId -- never References/In-Reply-To
        sender=Sender(
            address=from_address,
            display_name=UntrustedString(value=from_display_name),
            domain_authenticated=domain_auth,
            # NOT computed here. The allowlist lookup that decides identity trust is a
            # later epic (spec 07) -- this adapter must never guess trust, so it is
            # unconditionally False for every envelope produced here.
            identity_trusted=False,
            known_contact=False,
        ),
        subject=UntrustedString(value=headers.get("subject", "")),
        participants=_participants_from_headers(headers),
        payload_ref=payload_ref,
        payload_sha256=payload_sha256,
        provenance=Provenance(
            source_system_id=source_system_id,
            history_id=history_record["history_id"],
        ),
    )


def _extract_history_records(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a raw `history.list` response into per-message `to_envelope` inputs."""
    records: list[dict[str, Any]] = []
    for entry in response.get("history", []):
        history_id = entry["id"]
        for added in entry.get("messagesAdded", []):
            records.append(
                {"kind": "messageAdded", "history_id": history_id, "message": added["message"]}
            )
        for changed in entry.get("messagesChanged", []):
            records.append(
                {"kind": "messageChanged", "history_id": history_id, "message": changed["message"]}
            )
    return records


def poll_once(
    gmail: ToolClient,
    store: IngestStore,
    clock: Clock,
    account_ref: str,
    *,
    payload_store: LocalPayloadStore,
) -> None:
    """One poll cycle: read cursor, fetch changes, enqueue + advance cursor atomically.

    Exactly the pattern in spec 01 "Poller loop": no writes happen outside the single
    `store.transaction()` block below.
    """
    cursor = store.get_cursor(_SOURCE_SYSTEM, account_ref)
    args: dict[str, Any] = {"startHistoryId": cursor} if cursor is not None else {}
    response = gmail.call("history.list", args)

    records = _extract_history_records(response)
    envelopes = [
        to_envelope(record, account_ref, clock=clock, payload_store=payload_store)
        for record in records
    ]
    new_cursor = response["historyId"]

    with store.transaction() as tx:
        tx.enqueue_many(envelopes)
        tx.set_cursor(_SOURCE_SYSTEM, account_ref, new_cursor)
