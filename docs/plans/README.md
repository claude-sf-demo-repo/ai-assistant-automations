# Architecture plans — evolution index

This directory versions the architecture plan so we can see how the design evolves over time.
Each plan is dated and immutable once superseded; the newest is the authoritative one.
New versions are added as new dated files (never edited in place), and this index records
what changed and why.

## Versions (newest first)

| Version | Date | Status | File |
|---|---|---|---|
| **v2 (Hardened)** | 2026-09-07 | **Current / authoritative** | [`2026-09-07-v2-shadow-mode-email-spine.md`](2026-09-07-v2-shadow-mode-email-spine.md) |
| v1 (Shadow-Mode Email Spine) | 2026-09-02 | Superseded by v2 | [`2026-09-02-v1-shadow-mode-email-spine.md`](2026-09-02-v1-shadow-mode-email-spine.md) |

## What changed, and why

### v1 → v2 (three-reviewer hardening pass)

v1 captured the resolved forks (execution model, system of record, trust boundary) and the
shadow-mode walking skeleton. A three-reviewer hardening pass (adversarial-security,
AI/evaluation, SWE/testing) then folded in ~30 must-fixes. The load-bearing changes:

- **Reframed "shadow mode ≠ zero risk" → "low-egress."** v1 still has two live egress
  surfaces (Telegram digest, off-box escalation) and writes durable state from
  attacker-controlled content on day one. The real v1 harms are a **poisoned state store**
  and a **confident-but-wrong digest** — the architecture is designed around those.
- **Span-verification promoted to the load-bearing control** — simultaneously the injection
  wall and the anti-hallucination gate. Span *attachment* ≠ span *support*.
- **Mutation layer given authority, not just shape**: closed typed command set
  (`AssertFact`/`SupersedeFact`/`UpsertWorkObject`/`TransitionWorkObject`/`LinkProvenance`)
  + per-command invariant + **privileged-mutation class** the ingestion path can never emit.
- **Trust boundary split**: domain-authentication (SPF/DKIM/DMARC, computed) vs.
  identity-trust (user-curated allowlist, never auto-learned); thread membership by server
  thread-id; confusable-domain detection.
- **Single deterministic writer**: enqueue-only producers; one `ingest-worker`; per-thread
  Postgres advisory lock; fold by `occurred_at` (event-time) for out-of-order convergence.
- **Recall-first evaluation**: stratified full-stream held-out sample; actionable-class recall
  is the gating metric; audit-sample the dropped/FYI bucket; in-flow corrections are
  regression fixtures only, never an unbiased metric source.
- **Routing vs. gating separated**: route to a bigger model on model *uncertainty*
  (self-consistency, not raw scalar); gate actions on *trusted signals*. Escalation is an
  explicit predicate with a ~≤15% SLO; sensitive→HOLD (not auto-escalate off-box).
- **Digest hardened**: deterministic template (never model-rendered), escaped/defanged,
  attacker strings quoted-as-external, locked to one chat-id.
- **Substrate cut to a true walking skeleton**: transaction-time only (valid-time columns
  reserved nullable), graph store deferred (Graphiti/Neo4j treated as a disposable downstream
  index rebuilt by replay, not an edge transplant).

The full severity-tagged finding list lives in the v2 plan under
"Hardening pass — folded-in must-fixes."

## Conventions

- **Filename:** `YYYY-MM-DD-v<N>-<slug>.md`.
- **Superseding, not editing:** to revise the design, add a new dated file, flip the old one's
  status to *Superseded* here, and add a "what changed" entry above.
- **Specs vs. plans:** this directory holds whole-system plans. Per-subsystem contracts live in
  [`../specs/`](../specs/) and are versioned with the code they describe.
