# Backfill & Cold-Start

_Epic: E10 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Process the existing mail backlog safely to bootstrap the seed golden set and validate the
pipeline, without letting a mass-mislabeling event poison the state store. Backfill reuses the
**same `ingest-worker`** fed the **same queue**, rate-capped, tagged `origin=backfill`, and gated
by a **diff report** that a human reviews before backfilled work objects are allowed to populate
state.

## Scope

**In v1**
- Bounded, rate-capped backlog ingestion through the normal queue + worker (no separate code path).
- `origin=backfill` on every produced row.
- Dry-run diff report (what *would* be written) as a human gate before work objects populate.
- Seed golden-set production (~300–500 stratified threads) for spec 08.

**Deferred / out of scope**
- Continuous re-backfill / re-processing at scale.
- Autonomy actions on backfilled objects (autonomy pinned at 0; trust-age gating applies later).

## Interfaces & data contracts

```python
@dataclass(frozen=True)
class BackfillPlan:
    account_ref: str
    since: date; until: date
    max_rate_per_min: int              # rate cap / backpressure
    dry_run: bool = True               # default: produce diff, do not populate work objects

@dataclass(frozen=True)
class DiffReport:
    would_create_facts: int; would_create_work_objects: int
    by_label: dict[str, int]           # coarse-label distribution
    uncertain_count: int
    samples: list[DiffSample]          # stratified examples for human review
    ready_for_apply: bool              # flips true only after human sign-off
```

- Backfill enqueues envelopes with `origin=backfill`; the worker runs the identical pipeline
  (triage → classify → extract → mutation layer).
- In `dry_run`, the mutation layer computes intended commands and records them into the diff
  report **without** persisting work objects (facts may be staged but flagged), so mislabeling is
  visible **before** it enters authoritative state.
- Rate cap + backpressure prevent a backfill from starving live ingestion or tripping
  denial-of-wallet (spec 03/07).

### Cold-start / seed golden set

The dry-run's stratified samples are hand-labeled (self-consistency spot-checked) to form the
seed golden set (spec 08). The trust-bar gate (recall ≥ X, faithfulness ≥ Y) is measured against
this set before the digest is treated as trustworthy.

## Invariants

1. Backfill uses the same worker and queue as live ingestion — no divergent code path.
2. Every backfilled row carries `origin=backfill`.
3. Backfilled work objects populate authoritative state only after the diff report is human-approved.
4. Backfill is rate-capped and yields to live ingestion under backpressure.
5. Autonomy remains pinned at 0; backfilled objects gain no authority (trust-age gating, spec 07).

## Failure modes

- **Mass mislabel in dry-run** → visible in the diff report; not applied; classifier revisited.
- **Backfill starves live traffic** → rate cap + backpressure; live ingestion prioritized.
- **Partial run interruption** → resumable via the queue's idempotency (spec 01).

## Acceptance criteria

- [ ] Backfill enqueues onto the same queue and is processed by the same `ingest-worker`.
- [ ] Every backfilled row is tagged `origin=backfill`.
- [ ] A dry-run produces a `DiffReport` (counts, label distribution, uncertain count, stratified samples) and does **not** populate work objects.
- [ ] Work objects populate authoritative state only after the diff report is explicitly approved.
- [ ] Backfill respects the rate cap and yields to live ingestion under backpressure (no starvation, no denial-of-wallet trip).
- [ ] An interrupted backfill resumes without duplicating events (idempotency).
- [ ] The dry-run yields a stratified ~300–500-thread sample used to seed the golden set (spec 08).

## Test seams

- Backfill plan executed against a fixture mailbox on ephemeral Postgres; diff report asserted.
- Rate-cap/backpressure tested with a synthetic large backlog alongside live events.
- Resume tested by interrupting mid-run and re-enqueueing.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| Backfill is a distinct high-risk job (v1 brief) | same worker/queue, rate-capped, `origin=backfill`, diff-report human gate |
| `[H · AI]` Cold-start | dry-run stratified samples seed the golden set + trust-bar gate |
| `[M, folded]` Denial-of-wallet | rate cap + backpressure; yields to live ingestion |
| `[H · Security]` Poisoned store under autonomy | `origin=backfill` + trust-age gating; no retroactive authority |

## Open items

- Backfill window size and rate-cap defaults.
- Whether facts (not just work objects) are staged or fully withheld in dry-run.
