# Overview & Conventions

_Cross-cutting · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Defines the shared vocabulary every subsystem depends on: the Common Event Envelope, the
universal fact/edge row conventions, the injectable test seams, the repo layout, and the
non-negotiable **content-is-data-never-instructions** principle. Read this before any other
spec — the others reference these contracts rather than restating them.

## Scope

**In v1**
- Common Event Envelope (channel-agnostic; email is the only concrete channel).
- Universal fact/edge row column conventions (transaction-time bitemporal; valid-time reserved).
- Injectable seams: `ModelClient`, `ToolClient`, `Clock`, `Store`.
- Repo layout, Python/pydantic-settings/Alembic conventions.
- The trust-boundary vocabulary (`domain_authenticated` vs `identity_trusted`, untrusted-content flag).

**Deferred / out of scope**
- Non-email channels (WhatsApp/iMessage/Instagram/Messenger) — envelope is designed to admit them.
- Valid-time (as-of) queries — columns reserved nullable, not populated.
- Graphiti/Neo4j projection — a disposable downstream index, rebuilt by replay later.

## Interfaces & data contracts

### Content-is-data-never-instructions (the root principle)

Every provider-supplied string — body, subject, display name, filename, header value — is
**untrusted data**. It is never concatenated into an instruction position of any prompt; it is
passed inside clearly delimited untrusted-content framing. Identity is asserted **only** by
verified address, never by any model reading of content. This principle is enforced structurally
by the mutation layer (spec 02), not by detection.

### Common Event Envelope

The single shape every adapter normalizes to. Core imports nothing provider-specific.

```jsonc
{
  "event_id": "uuid",                       // internal, assigned on ingest
  "idempotency_key": "gmail:msg:<id>|message.received", // = source_system_id + "|" + type
  "source_system": "gmail",
  "channel": "email",
  "type": "message.received",               // message.received | message.replied | thread.updated ...
  "account_ref": "personal",                // on EVERY envelope; scopes identity/trust
  "occurred_at": "2026-09-07T09:12:00Z",    // event-time (authoritative for folding/ordering)
  "ingested_at": "2026-09-07T09:14:03Z",    // wall-clock we saw it
  "thread_ref": "gmail-thread-id",          // SERVER-assigned thread id (never References header)
  "sender": {
    "address": "a@example.com",             // verified envelope address — the ONLY identity key
    "display_name": {"value": "A. Sender", "untrusted": true},
    "domain_authenticated": {"spf": "pass", "dkim": "pass", "dmarc": "pass"}, // computed
    "identity_trusted": false,              // user-curated allowlist lookup — NEVER auto-learned
    "known_contact": false                  // non-transitive; gates NOTHING consequential
  },
  "subject": {"value": "Re: invoice", "untrusted": true},
  "participants": [                          // typed list for owner coreference (spec 04)
    {"address": "a@example.com", "role": "from"},
    {"address": "me@…", "role": "to"}
  ],
  "payload_ref": "store://raw/<hash>",       // pointer to raw content; NOT inlined as instructions
  "payload_sha256": "…",
  "provenance": {"source_system_id": "gmail_msg_id", "history_id": "…"}
}
```

Rules:
- `idempotency_key` = `source_system_id + "|" + type`; a unique constraint enforces exactly-once.
- Any string that originated from the sender carries `{"value": …, "untrusted": true}`.
- `domain_authenticated` is **computed** (auth results); `identity_trusted` is **read from the
  user-curated allowlist**. These are different axes and must never be conflated (spec 07).

### Universal fact / edge row conventions

Every fact/edge row in state (spec 02) carries:

| Column | Meaning |
|---|---|
| `subject`, `predicate`, `object` | the triple |
| `recorded_at` | transaction-time: when we learned it (v1 authoritative) |
| `invalidated_at` | transaction-time: when superseded (NULL = current). Append-only. |
| `valid_from`, `valid_to` | valid-time: reserved **nullable**, unpopulated in v1 |
| `account_ref` | scoping key on **every** row — no cross-context leaks |
| `source_event_id` | the envelope that produced it |
| `source_span` | `{message_id, start, end}` — the cited span (spec 02 span-verification) |
| `confidence` | per-field, from self-consistency (spec 04), not raw model scalar |
| `source_system` | `gmail` etc. |
| `origin` | `live` \| `backfill` \| `human` \| `correction` |
| `correction_reason` | set on compensating supersede writes; NULL otherwise |

Superseded facts are **invalidated, not deleted** (`invalidated_at` set, new row inserted).

### Injectable seams (test doubles for all I/O)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...            # no wall-clock reads outside this

class ModelClient(Protocol):                   # spec 03
    def generate(self, req: ModelRequest) -> ModelResponse: ...

class ToolClient(Protocol):                    # external side-effecting calls (Gmail, Telegram)
    def call(self, tool: str, args: dict) -> dict: ...

class Store(Protocol):                          # spec 02 persistence boundary
    def enqueue(self, item: QueueItem) -> None: ...
    def apply(self, cmd: MutationCommand) -> MutationResult: ...
    # ... repositories are pure persistence; no business logic
```

Everything that touches the network, disk, or clock goes through one of these so tests are
deterministic. Local and hosted model tiers are both fakeable.

## Invariants

1. No provider string ever reaches an instruction position of a prompt; all carry the untrusted flag.
2. Identity is keyed on verified `sender.address` only — never display name, never content.
3. `account_ref` is present on every envelope and every state row.
4. `occurred_at` (event-time) is authoritative for ordering/folding; `ingested_at` is diagnostic only.
5. Every I/O crosses a seam (`Clock`/`ModelClient`/`ToolClient`/`Store`); nothing reads wall-clock or network directly.
6. `idempotency_key` uniquely identifies an event; re-delivery is a no-op.

## Failure modes

- **Adapter emits a malformed envelope** → rejected at validation (spec 01), never partially applied.
- **Missing `account_ref`** → hard validation error (fail-closed), never defaulted silently.
- **Untrusted flag absent on a provider string** → schema rejects the envelope.

## Acceptance criteria

- [ ] A pydantic model for the Common Event Envelope validates the schema above, rejecting envelopes missing `account_ref`, `idempotency_key`, or untrusted flags on provider strings.
- [ ] `domain_authenticated` and `identity_trusted` are distinct fields; no code path derives trust from auth results.
- [ ] `Clock`, `ModelClient`, `ToolClient`, `Store` protocols exist and every I/O call site depends on the protocol, not a concretion.
- [ ] A fact/edge base row model includes all universal columns, with `valid_from`/`valid_to` nullable and unset in v1.
- [ ] Repo layout matches the plan (`adapters/`, `core/…`, `digest/`, `shared/…`, `storage/…`, `tests/{unit,integration,evals,adversarial}`).
- [ ] `.env.example` + pydantic-settings config module load all settings; no secret is read from a hard-coded literal.
- [ ] Alembic is configured forward-only; the initial migration creates the fact/edge base table with the universal columns.

## Test seams

- All four protocols have in-memory fakes in `tests/`.
- A `FakeClock` makes time deterministic; a `FakeModelClient` returns scripted responses (incl. scripted self-consistency disagreement).
- Envelope validation has a unit suite of malformed/injection fixtures.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[C · Security]` Trusted-signal boundary spoofable | `domain_authenticated` vs `identity_trusted` split; identity by verified address only; `thread_ref` = server id |
| `[M, folded]` Metadata is an un-neutralized injection site | untrusted flag on subject/display-name/filename/header |
| `[H · Security]` Cross-context leak on account #2 | `account_ref` mandatory on every envelope and row; context-scoped |
| `[C · SWE]` Substrate over-scoped | valid-time columns reserved nullable, unpopulated |
| `[L, folded]` Reproducibility | seams make model/clock deterministic in tests |

## Open items

- Final `type` enumeration for the envelope beyond `message.received`/`message.replied`.
- `payload_ref` store backend (filesystem vs object store) and its retention/TTL wiring (spec 07/09).
