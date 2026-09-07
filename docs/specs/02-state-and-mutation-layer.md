# State & Mutation Layer

_Epic: E2 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

The single writer of durable state and the load-bearing security control. All state changes flow
through a **closed set of typed mutation commands**, each validated by a pure function with one
invariant, gated by **span-verification** (a field's value must be supported by its cited span),
and screened by a **privileged-mutation class** the ingestion path can never emit. State is
transaction-time bitemporal, append-only (invalidate-not-delete), and account-scoped.

## Scope

**In v1**
- Transaction-time bitemporal schema (`recorded_at`/`invalidated_at`); valid-time columns reserved nullable.
- Closed command set: `AssertFact`, `SupersedeFact`, `UpsertWorkObject`, `TransitionWorkObject`, `LinkProvenance`.
- Span-verification (extractive byte-match; abstractive entailment-checked).
- Privileged-mutation class (rejected from the ingestion path).
- Append-only supersede; provenance-driven invalidation cascade on correction.
- Typed conflict rules; fact-type → volatility TTL map.
- Account-scoped entity identity.
- Hash-chained audit hook (spec 07 owns the chain; this spec emits into it).

**Deferred / out of scope**
- Valid-time (as-of) queries.
- Semantic dedup / NLI contradiction detection (fast-follow).
- Graphiti/Neo4j projection.

## Interfaces & data contracts

### Schema (sketch)

```sql
CREATE TABLE fact (
  id uuid PRIMARY KEY,
  account_ref text NOT NULL,
  subject text NOT NULL, predicate text NOT NULL, object jsonb NOT NULL,
  fact_type text NOT NULL,
  recorded_at timestamptz NOT NULL,           -- transaction-time (v1 authoritative)
  invalidated_at timestamptz,                  -- NULL = current
  valid_from timestamptz, valid_to timestamptz,-- reserved nullable, unused v1
  source_event_id uuid NOT NULL,
  source_span jsonb NOT NULL,                  -- {message_id,start,end}
  confidence real,
  source_system text NOT NULL,
  origin text NOT NULL,                         -- live|backfill|human|correction
  correction_reason text,
  ttl_expires_at timestamptz                    -- from volatility map
);
CREATE INDEX ON fact (account_ref, subject, predicate) WHERE invalidated_at IS NULL;
```

Work objects live in their own table (spec 05) but are only mutated through this layer.

### Closed command set

Each command is `(JSON schema, pure validation fn, one invariant)`:

```python
@dataclass(frozen=True)
class AssertFact:      # invariant: referential integrity + span-verification
    account_ref: str; subject: str; predicate: str; object: dict
    fact_type: str; source_event_id: str; source_span: Span; confidence: float

@dataclass(frozen=True)
class SupersedeFact:   # invariant: target fact exists & is current; append-only
    target_fact_id: str; reason: str; source_event_id: str

@dataclass(frozen=True)
class UpsertWorkObject:  # invariant: idempotency-key uniqueness
    account_ref: str; wo_type: str; idem_key: str; fields: dict; source_span: Span

@dataclass(frozen=True)
class TransitionWorkObject:  # invariant: legal state transition (spec 05 FSM)
    wo_id: str; to_state: str; source_event_id: str; source_span: Span

@dataclass(frozen=True)
class LinkProvenance:   # invariant: both endpoints exist
    from_id: str; to_id: str; relation: str

MutationCommand = AssertFact | SupersedeFact | UpsertWorkObject | TransitionWorkObject | LinkProvenance
```

Single entry point:

```python
def apply(cmd: MutationCommand, *, path: Literal["ingestion","human","admin"]) -> MutationResult:
    validate_schema(cmd)                    # shape
    reject_if_privileged(cmd, path)         # authority (see below)
    verify_span(cmd)                        # support, not just resolution
    check_invariant(cmd)                    # per-command
    return persist(cmd)                     # append-only; emits audit entry
```

### Span-verification (the load-bearing control)

Span *attachment* (the span resolves) is **not** span *support*. For every extractive field:

```python
def verify_span(cmd) -> None:
    span_text = load_span(cmd.source_span)         # deterministic slice of raw payload
    for field, value in extractive_fields(cmd):
        if not byte_supported(value, span_text):    # value is a deterministic fn of the span
            raise SpanUnsupported(field)
    for field, value in abstractive_fields(cmd):
        if not entailed(value, span_text):          # NLI/entailment check
            raise NotEntailed(field)
```

A schema-valid but mis-grounded command (e.g. a value not present in its cited span) is
**rejected**, regardless of model confidence.

### Privileged-mutation class

Mutations touching **identity/trust, policy/autonomy, or money/credentials/contact-changes** are
privileged. `reject_if_privileged` raises if such a command arrives on `path="ingestion"`. They
are emittable only via `human`/`admin` paths. Consequence-bearing mutations are quarantined
regardless of model confidence — attacker-chosen but schema-valid content (e.g.
`Task{action:"wire $5000 to IBAN…"}`) cannot become an authoritative privileged fact.

### Correction → invalidation cascade

Corrections are **compensating supersede writes** (`origin="correction"`, `correction_reason`,
batch id) — never physical deletes. Superseding a fact triggers a **provenance-driven cascade**:
dependent summaries/work objects linked via `LinkProvenance` are recomputed. Superseded rows are
later compacted to a cold partition.

### Conflict rules & volatility TTL

- **Typed conflict rule**: same `(account_ref, subject, predicate)` slot with a new `object`
  value = a deterministic conflict → supersede the prior, record both (no NLI needed in v1).
- **Volatility map**: `fact_type → TTL`; on expiry a fact is flagged stale (drives
  `needs_full_thread_read`, spec 04), not auto-deleted.

## Invariants

1. `apply()` is the only path that writes state.
2. Every persisted extractive field is byte-supported by its cited span; every abstractive field is entailed by it.
3. No privileged mutation is ever persisted via the ingestion path.
4. Facts are never physically deleted in the hot path; supersede sets `invalidated_at` and inserts a new row.
5. Every command carries `account_ref`; no write crosses account contexts.
6. Every applied command emits exactly one hash-chained audit entry (spec 07).
7. Each command satisfies its single declared invariant or is rejected atomically.

## Failure modes

- **Mis-grounded field** → `SpanUnsupported`/`NotEntailed`; command rejected, nothing persisted.
- **Illegal work-object transition** → rejected (spec 05 FSM).
- **Duplicate `idem_key`** → upsert no-ops / merges deterministically.
- **Privileged command from ingestion** → hard reject + audit of the attempt.
- **Correction of a non-current fact** → rejected (must target a current fact).

## Acceptance criteria

- [ ] All five commands exist as frozen dataclasses with JSON schemas and pure validation functions.
- [ ] `apply()` is the sole writer; repositories expose no other write method.
- [ ] An extractive value absent from its cited span is rejected (byte-match test).
- [ ] An abstractive claim not entailed by its span is rejected (entailment test with a fake judge).
- [ ] A privileged mutation (identity/trust, policy/autonomy, money/credential/contact) submitted on `path="ingestion"` is rejected and the attempt is audited.
- [ ] The same privileged mutation on `path="admin"` is accepted.
- [ ] Superseding a fact sets `invalidated_at`, inserts a new current row, and never deletes.
- [ ] A correction supersede write triggers recomputation of provenance-linked dependents.
- [ ] Two facts for the same slot with different values produce a deterministic conflict → prior superseded.
- [ ] Every applied command produces exactly one audit entry; every row carries `account_ref`.
- [ ] valid-time columns exist and remain NULL in v1.

## Test seams

- Pure validation functions are unit-tested with no I/O.
- `verify_span` takes an injected entailment judge (`ModelClient`), faked deterministically.
- Cascade + conflict tests run on ephemeral Postgres.
- Adversarial fixtures (spec 08) feed injected/mis-grounded/privileged commands and assert rejection.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[C · Sec+AI]` Span-verification is load-bearing | `verify_span`: attachment ≠ support; extractive byte-match + abstractive entailment |
| `[C · Security]` Mutation layer validates shape not authority | privileged-mutation class rejected from ingestion path; consequence-bearing quarantined |
| `[H · SWE]` Mutation layer unbounded catch-all | closed 5-command set, each with schema + pure fn + one invariant |
| `[M, folded]` Silent state corruption is the real v1 harm | correction = compensating supersede + provenance-driven invalidation cascade |
| `[M, folded]` Typed conflict rules + volatility TTL | deterministic slot-conflict rule; `fact_type→TTL` map |
| `[H · Security]` Cross-context leak | `account_ref` on every row; account-scoped identity |
| `[H · Security]` Poisoned store durable under autonomy | append-only + provenance + trust-age fields feed per-object gating (spec 07) |

## Open items

- Entailment judge choice for abstractive fields (local vs escalated; ECE budget — spec 04).
- Cold-partition compaction cadence for superseded rows.
- Exact `fact_type → TTL` table values.
