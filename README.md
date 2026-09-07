# AI Assistant Automations — Shadow-Mode Email Spine

An event-driven, self-hosted (cloud-assisted) AI personal assistant that ingests email,
builds durable **grounded, bitemporal state**, and renders a **daily digest over Telegram** —
so the human stops managing an inbox and instead sees only decisions, approvals, exceptions,
waiting items, and FYIs. This is the first component of the broader **Hermes Agent** vision.

> **v1 is shadow mode: zero email actions.** It observes, classifies, extracts structured
> work objects, and reports. It builds the golden set that later unlocks autonomy, on a
> provider-agnostic, graph-ready substrate.

## ⚠️ This is a public repository

Nothing sensitive belongs in git: **no OAuth tokens, secrets, credentials, or real mail
data — ever.** See [`.gitignore`](.gitignore). All docs here are architecture/design only.
The running system keeps secrets in a secrets store and stores refs/hashes rather than raw
content wherever possible.

## Guiding principle

Convert events into durable structured state once, then operate on state. Deterministic rules
before models; cheap/local before expensive/hosted; cached grounded state before raw re-reads.

## Where to start

| You want to… | Go to |
|---|---|
| Understand the whole design | [`docs/plans/`](docs/plans/) — the versioned architecture plan (v1 → v2, evolving) |
| Read a subsystem contract | [`docs/specs/`](docs/specs/) — one spec per subsystem |
| Pick up build work | GitHub **Epics** and their child **Issues** (labels + `v1: Shadow-Mode Email Spine` milestone + Project board) |

## v1 walking skeleton

```
poll history.list → single-consumer ingest-worker →
  deterministic triage → thread-conditioned local coarse classify (k-sample, UNCERTAIN route) →
  escalation predicate (uncertainty→hosted; sensitive→HOLD) →
  grounded extraction (span + relative-expr) → deterministic date/owner normalizers →
  MUTATION LAYER (typed command + invariant + span-verification + privileged-class) →
  transaction-time bitemporal state + typed work objects (fold by occurred_at) →
  hash-chained audit → deterministic Telegram digest → in-flow corrections + seed golden set
```

## Repository layout (target)

```
docs/plans/     versioned architecture plans + evolution index
docs/specs/     per-subsystem specifications
adapters/       provider integrations behind the Common Event Envelope (v1: gmail)
core/           ingestion, triage, classify, router, extraction, state, mutation,
                workobjects, dedup, policy, audit
digest/         deterministic Telegram templater
shared/         schemas, events, logging, auth, observability, config
storage/        migrations (Alembic, forward-only), repositories
tests/          unit, integration, evals, adversarial
```

## Status

Planning complete; build not started. Handoff target for the build is Sonnet.
