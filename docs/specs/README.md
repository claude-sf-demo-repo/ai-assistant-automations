# Subsystem specifications

Per-subsystem build contracts for the v1 Shadow-Mode Email Spine. Each spec is the
authoritative interface + acceptance contract for one subsystem and maps 1:1 to a GitHub
**Epic**. Read the [architecture plan](../plans/) first for the whole-system picture; these
specs decompose it into buildable units.

Every spec follows the same shape: **Purpose · Scope (in/out) · Interfaces & data contracts ·
Invariants · Failure modes · Acceptance criteria · Test seams · Reviewer-finding traceability ·
Open items.**

| # | Spec | Epic | Subsystem |
|---|---|---|---|
| 00 | [overview-and-conventions](00-overview-and-conventions.md) | — (cross-cutting) | Common Event Envelope, schema conventions, injectable seams, repo layout |
| 01 | [ingestion-and-event-backbone](01-ingestion-and-event-backbone.md) | E1 | Poller, envelope, idempotency, DLQ, work queue, single-writer |
| 02 | [state-and-mutation-layer](02-state-and-mutation-layer.md) | E2 | Bitemporal state, typed mutation commands, span-verification, privileged class |
| 03 | [model-router-and-local-inference](03-model-router-and-local-inference.md) | E3 | ModelClient, Router, CostMeter, escalation predicate, Ollama |
| 04 | [classification-and-extraction](04-classification-and-extraction.md) | E4 | Coarse classify, self-consistency, grounded extraction, deterministic normalizers |
| 05 | [work-objects](05-work-objects.md) | E5 | Typed state machines, event-time folding |
| 06 | [digest-and-telegram](06-digest-and-telegram.md) | E6 | Deterministic templater, defanging, one chat-id, corrections capture |
| 07 | [security-and-trust-boundary](07-security-and-trust-boundary.md) | E7 | Domain-auth vs identity-trust, kill-switch, denial-of-wallet, secrets |
| 08 | [evaluation-and-golden-set](08-evaluation-and-golden-set.md) | E8 | Recall-first eval, faithfulness, calibration, adversarial suite |
| 09 | [ops-and-platform](09-ops-and-platform.md) | E9 | docker-compose, systemd, backups, config, supply chain |
| 10 | [backfill-and-cold-start](10-backfill-and-cold-start.md) | E10 | Rate-capped backfill, diff report, seed golden set |

## Conventions

- Specs are versioned alongside the code they describe (edit in place; git history is the record).
- Whole-system design changes go in [`../plans/`](../plans/) as new dated versions, not here.
- Acceptance criteria are written to be directly liftable into GitHub issue checklists.
