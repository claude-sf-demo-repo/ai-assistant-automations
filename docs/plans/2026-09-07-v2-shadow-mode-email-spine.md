# AI Personal Assistant — Architecture Plan v2 (Hardened) — Shadow-Mode Email Spine

_Status: **Current / authoritative.** Supersedes [v1 (2026-09-02)](2026-09-02-v1-shadow-mode-email-spine.md). See the [plan index](README.md) for how the design has evolved._

## Context

**Problem.** The planning brief proposes turning Gmail/Calendar/Drive/meetings into event sources for an AI-operated personal operating system, so the human stops managing an inbox and instead sees only decisions, approvals, exceptions, waiting items, and FYIs. The brief is a strong capability catalogue (14 capabilities, ~180 open questions) but not yet an architecture: execution model, system of record, and trust boundary were deferred, and it reads as "build all six services at once."

**What prompted this.** A collaborative brainstorming/review session, followed by a three-reviewer hardening pass (adversarial-security, AI/evaluation, SWE/testing). This v2 folds in the corroborated must-fixes.

**Intended outcome.** A **lightweight, self-hosted, cloud-assisted v1** running in **shadow mode**: ingest email events, classify cheaply and locally, build durable **grounded, bitemporal state**, extract structured **work objects**, record a full audit trail, and render a **daily digest over Telegram**. It builds the golden set that later unlocks autonomy, on a provider-agnostic, graph-ready substrate.

> **Reframing (hardening pass, corroborated by Security + AI reviewers): shadow mode is _low-egress_, not zero-risk.** v1 takes no email actions, but it has **two live egress surfaces** — the Telegram digest and off-box escalation to the hosted model — and it **writes durable state from attacker-controlled content from day one**. The real v1 harms are (a) a **poisoned state store** and (b) a **confident, grounded-looking but wrong digest**. The architecture is designed around those, not around "no send = safe."

**Guiding principle (endorsed):** convert events into durable structured state once, then operate on state. Deterministic rules before models; cheap/local before expensive/hosted; cached grounded state before raw re-reads.

---

## Resolved decisions

| Decision | Choice |
|---|---|
| v1 autonomy | **Shadow mode, zero email actions.** Policy/audit/reversibility scaffolded, pinned at level 0. Autonomy later gates **per-object** on provenance/trust-age, never a single global switch. |
| Model access | **Router abstraction, multi-backend.** Split into `ModelClient` (per-backend transport), `Router` (pure stage→backend policy), `CostMeter`. |
| Cheap/bulk tier | **Local model on the box (Ollama)** for a **coarse** taxonomy; **hosted strong tier on escalation** only, with a minimized/redacted payload. |
| Orchestration | **n8n = transport only, holds no tokens and no mail**; it enqueues into a Postgres work queue. Core (Python) holds OAuth + fetches messages. Postgres authoritative. |
| State format | **Transaction-time bitemporal in Postgres v1** (`recorded_at`/`invalidated_at`); **valid-time columns reserved nullable** for later. `account_ref` on **every fact/edge**. Append-only, invalidate-not-delete. |
| Graph future | **Graphiti/Neo4j is a downstream, rebuildable/disposable index** fed by replaying our episodes. Postgres stays authoritative. Migration is a replay, not an edge-table transplant. |
| Mutations | A **closed set of typed commands** through one **validated mutation layer** with per-command invariants + **span-verification** + a **privileged-mutation class** the ingestion path can never emit. |
| Account topology | **Single personal Google account** now; `account_ref` on every fact + **context-scoped** entity identity/trust so multi-inbox/non-Google is additive without cross-context leaks. |
| Core runtime | **Python.** |
| Digest surface | **Telegram**, **deterministic template (never model-rendered)**, locked to one authorized chat-id. → later scheduled AM/PM → voice message → voice call. |
| Hosting | **Self-hosted box.** v1 uses **polling** (no inbound endpoint). |
| Eval feedback | **Stratified full-stream held-out sample (recall-first)** + in-flow corrections (fixtures only, not metrics). |
| Ingestion | **Poll `history.list`**; `historyId` cursor persisted transactionally with the events it produces. |

---

## Hardening pass — folded-in must-fixes (severity; reviewer convergence noted)

**[C · Security+AI converge] Span-verification / faithfulness gate is the load-bearing control.** Span-*attachment* is not span-*support*. The mutation layer must verify each field against its cited span, not just that the span resolves. This is simultaneously the injection wall and the anti-hallucination gate. → *Architecture, `core/mutation`, `core/extraction`, Security model, Eval.*

**[C · Security] Mutation layer validates shape, not authority.** A schema-valid but attacker-chosen mutation (e.g. `Task{action:"wire $5000 to IBAN…"}`) currently passes. Add (i) span byte-match/entailment, (ii) a **privileged-mutation class** (identity/trust, policy/autonomy, money/credentials/contact-changes) the ingestion path can never emit — only human/admin paths, (iii) consequence-bearing mutations quarantined regardless of model confidence. → *`core/mutation`.*

**[C · Security] Trusted-signal boundary is spoofable/self-poisoning.** SPF/DKIM/DMARC prove *domain authentication*, not *identity trust* — an attacker's own domain passes all three. Split **domain-authenticated** from **identity-trusted (user-curated allowlist, never auto-learned)**; `known_contact` is non-transitive and gates nothing consequential; verify thread membership by **server-assigned Gmail thread id**, not `References` headers; add confusable/cousin-domain detection; a new participant resets thread trust. → *Envelope, Security model.*

**[C · Security] Digest is an egress + phishing surface.** Render with a **deterministic templater, never a model**; escape and strip bidi/control chars; **defang URLs** (no auto-links); show attacker-derived strings as *quoted external content*, never in assistant voice; cap per-item length; lock the bot to one authorized chat-id (its only output). → *`digest/`.*

**[C · Security] Attacker controls what escalates → targeted off-box exfiltration.** Escalation = content leaving the box, and "sensitive → escalate" means the most sensitive mail leaks most. Escalation payload = **minimum necessary, PII-redacted**, never auto-attaching cross-thread/PII state; **"sensitive" is a hold-for-human branch, not auto-escalate**; log every off-box payload. → `core/router`.

**[C · AI] The eval loop is structurally blind to misses.** In-flow corrections are selection-biased (you can only correct what surfaced), so they measure precision and are blind to **recall** — the safety-critical metric. Held-out sample must be a **stratified random draw of ALL inbound (incl. no-action/FYI)**, independently labeled; **recall on the actionable class is the gating metric**; periodically **audit-sample the dropped/FYI bucket** to estimate miss rate. In-flow corrections are regression fixtures, explicitly not an unbiased metric source. → *Eval.*

**[C · AI] Escalation function is undefined and the plan conflates two decisions.** *Routing to a bigger model* on model uncertainty is safe (no payoff in forging *low* confidence); *gating an action* must use trusted signals. Write escalation as an explicit testable predicate with an **escalation-rate SLO (~≤15%)**; on cap-hit **fail-closed** to UNCERTAIN in the digest. → *`core/router`.*

**[C · SWE] No runtime owns the "one deterministic writer."** Make writes single-consumer: n8n/backfill/poller only **enqueue**; one `ingest-worker` consumes; all mutations for a `thread_id` serialized by **Postgres advisory lock**. Explicit process inventory. → *Architecture.*

**[C · SWE] v1 substrate is over-scoped for "ASAP."** Cut to a walking skeleton: **transaction-time only** (valid-time columns reserved nullable), defer point-in-time queries, defer the graph store, keep the eval *design* + seed set but defer held-out depth. Zero rework because schema reserves the columns/fields now. → *Scope.*

**[H · Security] Autonomy will one day flip on a store poisoned during months of shadow mode.** Append-only makes poison durable. Gate autonomy **per-object** on provenance + **trust-age** (objects created while a sender was untrusted never retroactively gain authority); **red-team the state store**, not just the classifier, before any autonomy. → *Security model.*

**[H · Security] Unified KG bakes in a cross-context leak the moment account #2 is added.** Decide now: entity identity is **account-scoped**; `account_ref` on every fact/edge; default context-scoped retrieval and per-context trust; cross-context links are explicit, audited, opt-in. → *State schema.*

**[H · Security] Endpoint/component blast radius.** Poll (no inbound endpoint) for v1; if push is ever used, verify Google OIDC and let n8n forward only the raw notification. Run core / n8n / Ollama / Postgres as separate least-privileged users/containers. Encryption-at-rest is useless against RCE → minimize what the process can reach. → *Security model.*

**[H · SWE] "Projection to Graphiti" is a rewrite unless scoped.** Graphiti re-extracts entities/edges with its own LLM on ingest; our typed edges don't transplant. Treat it as a downstream disposable index. → *State, resolved above.*

**[H · SWE] Mutation layer is an unbounded catch-all as written.** Specify a closed command set: `AssertFact`, `SupersedeFact`, `UpsertWorkObject`, `TransitionWorkObject`, `LinkProvenance` — each a JSON schema + pure validation fn + one invariant (referential integrity / legal transition / idempotency-key uniqueness). → *`core/mutation`.*

**[H · SWE] Idempotency/DLQ/replay/ordering are named, not specified.** Idempotency key = `source_system_id + type` with a unique constraint; DLQ table + retry/backoff/poison threshold; **fold by `occurred_at` per aggregate (event-time ordering)** — not a timed buffer — so out-of-order converges deterministically. → *`core/ingestion`, `core/workobjects`.*

**[H · AI] Small-model self-reported confidence is miscalibrated.** Route on **self-consistency** (k=3–5 samples, label agreement/entropy), not the raw scalar; measure calibration (ECE) on the seed set; represent confidence **per-field**. → *`core/classify`.*

**[H · AI] No abstention path.** Add an explicit **UNCERTAIN** route to a budgeted digest section — in shadow mode abstaining is nearly free and is the richest cold-start labeling stream. Cap its size (rank by trusted-signal risk) to avoid alert fatigue. → *`core/classify`, `digest`.*

**[H · AI] Deadline/owner extraction is where 7–8B models fail.** LLM emits the **span + relative expression only**; a **deterministic normalizer** computes absolute dates (timezone + business-day + `date_precision`); owner resolved by **coreference against a typed participant list** with `owner_ambiguous` → UNCERTAIN. Never let the LLM do date arithmetic. → *`core/extraction`.*

**[H · AI] Classification needs thread conditioning.** "sounds good, go ahead" is inert per-message. Unit of record = per-message; unit of inference = thread-aware (condition on running summary + work-object state). → *`core/classify`.*

**[H · AI] Cold-start.** Seed golden set (~300–500 threads, stratified, self-consistency spot-checked) hand-labeled from the backlog dry-run; define a **trust-bar gate** (actionable-recall ≥ X, faithfulness ≥ Y) below which the digest is flagged low-trust. → *Eval, Verification.*

**[M, folded] Metadata is an un-neutralized injection site** (subject, display name, filenames, headers) — same untrusted framing as bodies; identity by verified address only. · **Silent state corruption is the real v1 harm** — faithfulness is a *gating* eval; corrections trigger a **provenance-driven invalidation cascade** that recomputes dependent summaries. · **Typed conflict rules** (same slot, new value = deterministic conflict) before any NLI; **fact-type→volatility TTL** map. · **Two-stage semantic dedup** (block on structured fields → cross-encoder/LLM-judge verify), never single-threshold auto-merge. · **Ops**: systemd `restart=always`, Telegram heartbeat if no successful poll in N min, `pg_dump` + restore drill, structured logging w/ rotation, `docker-compose` (Postgres+Ollama+n8n) + `make dev/test`, pydantic-settings + `.env.example`. · **Data-correction protocol** = compensating supersede writes (`correction_reason` + batch id), never physical delete; superseded rows compacted to cold partition. · **Supply chain**: pin+hash model artifacts and deps (lockfiles), SBOM, minimal n8n nodes. · **Privacy/GDPR**: subject-level purge across ALL stores incl. embeddings/audit/`model_runs`; store refs+hashes not raw where possible; hash-chain audit for tamper-evidence; TTL raw payloads separately from derived facts. · **Denial-of-wallet**: per-source-fair bounded queues + backpressure on the local-inference queue; global cap fails safe-and-degrade (deterministic-only + flag). · **Kill-switch**: authenticated, remotely reachable (verified chat-id), halts escalation + digest + ingestion. · **Golden-set poisoning**: corrections human-authenticated + provenance-tagged; held-out set curated separately and immutable to in-flow edits; corrections that would create/modify a *rule* need review.

**[L, folded]** Reproducibility: log prompt-template hash + model tag + sampling params in `model_runs`; run evals at temperature 0 / fixed seed; version prompts as artifacts. · Single-labeler quality: per-field labeling guidelines + delayed re-label overlap for self-consistency; low-agreement fields are aleatoric → UNCERTAIN. · Separate the model-hitting `evals`/`adversarial` suites from the inner-loop CI. · Note Telegram itself receives digest content (a third party).

---

## Architecture

```
 Google (Gmail)  ──poll history.list──►  poller  ──enqueue──►  Postgres work queue
                                                                     │
                    (n8n optional transport; holds NO tokens/mail)   │  LISTEN/NOTIFY + FOR UPDATE SKIP LOCKED
                                                                     ▼
 ┌──────────────── ingest-worker (single consumer, Python, on the box) ─────────────────┐
 │  per-thread advisory lock →                                                           │
 │  deterministic triage → thread-conditioned LOCAL coarse classify (k-sample) →         │
 │  escalation predicate (uncertainty→bigger model; sensitive→HOLD) →                    │
 │  grounded extraction (span + relative-expr) → deterministic normalizers (date/owner)→ │
 │  MUTATION LAYER: typed command + invariant + span-verification + privileged-class →   │
 │  bitemporal state + typed work objects (fold by occurred_at) → hash-chained audit     │
 │  policy engine + reversibility map present, pinned AUTONOMY_0                          │
 └───────────────────────────────────────────────────────────────────────────────────────┘
        │                         │                              │
        ▼                         ▼                              ▼
 Postgres (state+audit+       Model Router (local Ollama /  digest-scheduler → deterministic
 evals+queue; account_ref     hosted strong; minimized+     Telegram template (defanged, escaped,
 on every fact)               redacted off-box payload)      quoted-external), one chat-id
```

**Process inventory:** `poller` (1) → `ingest-worker` (1, the only DB writer via the queue) → `digest-scheduler` (1). n8n optional, transport only. Backfill is the *same* worker fed the *same* queue, rate-capped, `origin=backfill`, producing a diff report before it may populate work objects.

**Components (representative paths):**
- `adapters/gmail/` — behind a **Common Event Envelope** (channel-agnostic; core imports nothing Gmail-specific). Persists `historyId` transactionally with the events it produces.
- `core/ingestion/` — envelope validation, idempotency (`source_system_id+type`, unique constraint), DLQ + retry/backoff/poison threshold, replay, event-time ordering.
- `core/triage/` — deterministic no-model rules.
- `core/classify/` — thread-conditioned **coarse** local label (action / no-action / FYI / approval / **UNCERTAIN**), per-field uncertainty via **self-consistency**.
- `core/router/` — `ModelClient` + pure `Router` (escalation predicate + SLO, fail-closed on cap) + `CostMeter`; minimized/redacted off-box payloads; sensitive→HOLD.
- `core/extraction/` — extractive fields (value must be a deterministic function of the span) vs. abstractive (entailment-checked); emits span + relative expression; deterministic date/owner normalizers set `date_precision`/`owner_ambiguous`.
- `core/state/` — `repositories` (pure persistence) separate from a `state-folder`/`summarizer` that **emits mutation intents** (never writes directly); typed conflict rules; fact-type→volatility TTL; provenance-driven invalidation cascade.
- `core/mutation/` — the only writer: closed typed command set, per-command invariant, span-verification, privileged-mutation class.
- `core/workobjects/` — typed state machines, event-time-ordered/commutative transitions.
- `core/dedup/` — event idempotency (v1); two-stage semantic dedup (fast-follow).
- `core/policy/` — autonomy 0–4 + reversibility map + **per-object trust-age**; pinned at 0.
- `core/audit/` — hash-chained, append-only; logs prompt hash + model tag + sampling params + tokens + cost.
- `digest/` — deterministic templater; escaping/defanging; UNCERTAIN section (budgeted); provenance drill-down that never auto-fetches attacker content.
- `shared/{schemas,events,logging,auth,observability,config}` (pydantic-settings), `storage/{migrations(Alembic, forward-only),repositories}`, `tests/{unit,integration,evals,adversarial}`.

**Common Event Envelope:** as before, but `sender` splits `domain_authenticated {spf,dkim,dmarc}` (computed) from `identity_trusted` (user-curated allowlist lookup); `thread_ref` = server thread id; all provider strings (subject, display name, filenames, headers) carry the untrusted-content flag; `account_ref` present.

**State schema:** brief's data domains; every fact/edge row carries `subject/predicate/object`, `recorded_at`/`invalidated_at` (v1), `valid_from`/`valid_to` (**reserved nullable**), `account_ref`, `source_event_id`, `source_span`, `confidence`, `source_system`, `origin`, `correction_reason`. Superseded = invalidated, never deleted.

---

## v1 scope (walking skeleton) & fast-follow roadmap

**v1 (email only, deferrals applied):** poll → single-consumer worker → triage → coarse local classify (thread-conditioned, self-consistency, UNCERTAIN route) → grounded extraction with deterministic date/owner normalization → typed-command mutation layer with span-verification → transaction-time bitemporal state + typed work objects → hash-chained audit → deterministic Telegram digest → in-flow correction capture + seed golden set. **Zero email actions.** Deferred (columns/fields reserved now): valid-time queries, held-out labeling depth, graph store, semantic dedup, NLI contradiction detection.

**Fast-follow 1 — Attachments & documents.** Hash → dedup → **deterministic security gate on every URL and file _before any LLM sees it_** (type/size/nesting/zip-bomb limits, AV scan, link defusing/sandboxed resolution, password-file handling) → classify → store → extract → summarize → index (pgvector) → link. *Hard prerequisite.*

**Fast-follow 2 — Meeting output (provider-agnostic).** Evaluate existing transcript integrations vs. build; grounded extraction → **two-stage semantic dedup** (block → verify) against existing work objects (provenance-merge, reversible).

**Fast-follow 3 — Other channels.** WhatsApp, iMessage, Instagram, Messenger as new adapters onto the same envelope; core unchanged.

**Later (gated on eval + audit + state-store red-team):** per-object autonomy 1→4, grounded replies, follow-up/waiting engine, sender-aware routing; Graphiti/Neo4j downstream index + graph-language compiler; digest → voice; self-hosted mail/files/calendar adapters.

---

## Local-model portability boundary (corrected per hardening pass)

| Stage | v1 execution | Portable to low-intelligence local (7–8B)? |
|---|---|---|
| Deterministic triage | rules | N/A (already local) |
| Bulk classification | local | **Coarse taxonomy only** (action/no-action/FYI/approval/UNCERTAIN); fine typing escalates/deferred |
| Date/owner resolution | **deterministic normalizer, not the LLM** | Yes (LLM emits span+expression only) |
| Extractive fields (amounts, names, explicit statements) | local + span-verification | **Partial** — verified-extractive yes |
| Inferred owner / relative-deadline meaning | local, escalate on ambiguity | No without escalation |
| Thread-summary generation (abstractive) | constrained/extractive local; **escalate for high-consequence threads** | Partial |
| Ambiguity resolution / abstractive faithfulness | **hosted strong** on escalation | Not yet |

Depends on model size: a 30B-class MoE via Ollama softens the extraction/calibration verdicts materially vs. 7–8B. Target model to be named (see open items).

---

## Security model (v1 posture)

Content is data, never instructions (bodies **and** metadata). The mutation layer is the only writer, enforcing typed commands + invariants + **span-verification** + a privileged-mutation class the ingestion path can never emit. Trust = user-curated identity allowlist, not domain authentication; `known_contact` non-transitive, gates nothing consequential; confusable-domain detection; thread membership by server id. Digest is deterministic + defanged + one chat-id. Escalation payloads minimized/redacted, sensitive→HOLD, logged. Poll (no inbound endpoint); component isolation; secrets in a store, least-privilege scopes (`gmail.readonly`). Denial-of-wallet: per-source-fair queues + backpressure + fail-safe-and-degrade global cap. Autonomy gates per-object on trust-age; **red-team the state store before flipping**. Privacy: subject-level erasure across all stores incl. embeddings/audit/`model_runs`; hash-chained tamper-evident audit; supply-chain pinning/SBOM. Remotely-reachable authenticated kill-switch halting escalation+digest+ingestion.

## Token / cost efficiency

Deterministic → local-cheap → hosted-strong; most messages resolve before any hosted call. Escalation-rate SLO monitored as a first-class cost metric. Cached grounded state avoids recomputation; `needs_full_thread_read` gated. Every call logs tokens+cost+prompt-hash+model+params.

## Testing & evaluation

- **Unit**: triage rules, envelope validation, idempotency, state transitions incl. out-of-order convergence, mutation-layer rejection of malformed/injected/mis-grounded intents.
- **Integration**: end-to-end email→digest on a **real ephemeral Postgres** (testcontainers/template DB), replay & DLQ, degradation paths. Seams: `ModelClient`, `ToolClient`, **`Clock`**, **`Store`** all injectable; local + hosted tiers fakeable deterministically.
- **Bitemporal golden test**: shuffled event order → folded "current" state (and, when enabled, as-of snapshot) match a fixture.
- **Evals (recall-first)**: gating metric = **actionable-class recall** on a stratified full-stream held-out sample; **faithfulness = fraction of claims entailed by their cited span**; calibration (ECE) on the seed set; audit-sample the dropped bucket for miss rate. In-flow corrections are regression fixtures only.
- **Adversarial suite (CI gate before any autonomy)**: injection in bodies/subjects/display-names/filenames; assert no unintended/mis-grounded mutation, trusted signals never overridden, digest neutralization, escalation never leaks cross-thread content.

## Verification (v1)

1. Envelope: recorded Gmail payloads → well-formed envelopes; `domain_authenticated` computed correctly; identity-trust read only from the allowlist; metadata flagged untrusted.
2. Single-writer & idempotency: replay same event twice → one record; shuffled `received`/`replied` → correct folded state via `occurred_at`; concurrent enqueue → serialized by advisory lock, no double-invalidation.
3. Classify/route: fixtures for deterministic-only / local / escalation; self-consistency drives routing; sensitive→HOLD (not off-box); cap-hit → fail-closed UNCERTAIN.
4. Extraction & mutation: extractive value must byte-match its span (reject on mismatch); dates via normalizer with `date_precision`; ambiguous owner→UNCERTAIN; injection fixture → mutation layer rejects; privileged mutation from ingestion path → rejected.
5. State & digest: labeled backlog dry-run (diff report gate) → deterministic Telegram digest (decisions / waiting / completed-would-have / **uncertain** / FYI) with safe drill-down; audit shows observe-only; a correction triggers the invalidation cascade and recomputes dependent summaries.
6. Evals & cost: seed golden set labeled → baseline recall + faithfulness + ECE recorded against the trust-bar gate; adversarial suite passes; synthetic flood → per-source fairness + fail-safe-and-degrade cap + Telegram heartbeat/kill-switch all trip.

## Open items (non-blocking)

- **Name the local model** (drives portability/calibration verdicts): recommend a 30B-class MoE in Ollama if the box allows; else 7–8B with coarse-only classification. Sizing depends on box RAM/GPU.
- Held-out sample size + labeling cadence + per-field labeling guidelines.
- Secrets mechanism (age/SOPS vs OS keychain).
- Whether `hermes-agent-utilities/models-api-client` (Salesforce Models API) registers as one hosted backend behind the router.
- Confusable-domain detection library/approach; date normalizer choice (dateparser/duckling-class).
