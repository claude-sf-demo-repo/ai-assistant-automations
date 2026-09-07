# Backfill & Cold-Start

_Epic: E10 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Process a bounded historical backlog to bootstrap the state store and produce the seed golden set — without mass-mislabeling the store or triggering runaway cost. Backfill is **the same `ingest-worker` fed the same queue**, rate-capped and tagged `origin=backfill`, and it must produce a **diff report** for human review before it is permitted to populate work objects.

## Scope

**In v1**
- Bounded backlog dry-run through the normal ingestion → classify → extract → mutation pipeline.
- Rate-capping and `origin=backfill` tagging on every produced event/fact.
- A diff report gate: backfill reaches durable work objects only after human review.
- Hand-labeling of the seed golden set (~300–500 stratified threads) from the dry-run (see spec 08).
- Per-object provenance + trust-age captured on every backfilled object.

**Deferred / out of scope**
- Unbounded / continuous historical reprocessing.
- Any autonomy (pinned at 0 throughout).
- Attachment/document backfill (fast-follow 1).

## Interfaces & data contracts

Backfill reuses the ingestion contracts (spec 01) and the mutation layer (spec 02). It adds only a driver, a rate cap, an origin tag, and a gated diff report.

```python
class BackfillRequest(BaseModel):
    account_ref: str
    since: datetime | None
    until: datetime | None
    max_messages: int                 # bounded
    rate_limit_per_min: int           # rate cap (denial-of-wallet + API politeness)
    origin: Literal["backfill"] = "backfill"

class BackfillDiffReport(BaseModel):
    request: BackfillRequest
    messages_seen: int
    proposed_facts: int
    proposed_work_objects: int
    class_histogram: dict[str, int]   # action/no-action/fyi/approval/uncertain counts
    uncertain_examples: list[ProvenanceRef]
    escalations_avoided: int          # sensitive → HOLD, not off-box (spec 03)
    approved: bool = False            # must be flipped by a human before population
```

**Two-phase execution:**
1. **Dry-run (default):** enqueue backlog events tagged `origin=backfill`, run the full pipeline, but write proposals to a staging area and emit `BackfillDiffReport`. No durable work objects.
2. **Populate (gated):** only after `approved=True`, replay staged mutations through the mutation layer into durable state; every object carries `origin=backfill` provenance and its sender trust-age.

## Invariants

1. Backfill uses the same single-consumer `ingest-worker` and the same mutation layer — no bypass path.
2. Every backfilled event/fact/object is tagged `origin=backfill` and carries provenance + trust-age.
3. Backfill is rate-capped and bounded by `max_messages`.
4. Durable work objects are populated only after human approval of the diff report.
5. Autonomy remains pinned at 0 for the entire backfill.
6. Sensitive items during backfill go to HOLD (never auto-escalated off-box).
7. Idempotency holds: re-running backfill over the same messages produces no duplicate records (spec 01 idempotency key).

## Failure modes

- **Rate cap exceeded / API throttling** → back off; never burst.
- **Diff report shows anomalous class distribution** → do not approve; re-tune before populating.
- **Partial populate interrupted** → resumable via idempotency keys; no duplicates on retry.
- **Backfill overlaps live ingestion** → per-thread advisory lock (spec 01) serializes writes; event-time folding (`occurred_at`) converges ordering.

## Acceptance criteria

- [ ] Backfill runs through the same `ingest-worker` + mutation layer (no bypass), tagged `origin=backfill`.
- [ ] Backfill is bounded by `max_messages` and rate-capped by `rate_limit_per_min`.
- [ ] Dry-run produces a `BackfillDiffReport` and writes no durable work objects.
- [ ] Durable population occurs only after the diff report is human-approved.
- [ ] Every backfilled object carries provenance + sender trust-age; autonomy stays at 0.
- [ ] Sensitive items during backfill are held for human review, never escalated off-box.
- [ ] Re-running backfill over the same messages is idempotent (no duplicates).
- [ ] The seed golden set (~300–500 stratified threads) is hand-labeled from the dry-run output (feeds spec 08).

## Test seams

- Backfill driver tested against a fixture backlog with `ModelClient` faked (deterministic classes).
- `Clock` injected; `Store` is an ephemeral Postgres (spec 09) for the populate path.
- Idempotency asserted by double-running the same fixture backlog.
- Diff-report gate asserted: population is refused while `approved=False`.

## Reviewer-finding traceability

| Finding (plan) | How this spec addresses it |
|---|---|
| `[H · Security] Autonomy flips on a poisoned store` | Diff-report gate + human approval before population; `origin=backfill` + trust-age on every object; autonomy pinned 0. |
| `[C · SWE] Single deterministic writer` | Backfill is the same worker + queue, not a parallel writer; per-thread advisory lock. |
| `[H · SWE] Idempotency/DLQ/replay/ordering` | Idempotency key prevents duplicates; event-time (`occurred_at`) folding converges backfill vs live ordering. |
| `[C · Security] Attacker controls what escalates` | Sensitive → HOLD during backfill (no off-box escalation). |
| `[M, folded] Denial-of-wallet` | Rate cap + bounded `max_messages`. |
| `[H · AI] Cold-start` | Seed golden set hand-labeled from the bounded dry-run. |

## Open items

- Backlog window (`since`/`until`) and `max_messages` defaults for the first dry-run.
- Diff-report review UX (CLI vs a simple report artifact).
- Stratification strategy for selecting the ~300–500 golden threads from the dry-run.
