# AI Personal Assistant — Architecture Plan (v1: Shadow-Mode Email Spine)

## Context

**Problem.** The planning brief (`~/Downloads/ai_personal_assistant_automation_planning_brief.md`) proposes turning Gmail/Calendar/Drive/meetings into event sources for an AI-operated personal operating system, so the human stops managing an inbox and instead sees only decisions, approvals, exceptions, waiting items, and FYIs. The brief is a strong, disciplined **capability catalogue** (14 capabilities, ~180 open questions, 6 services) but is not yet an architecture: the three load-bearing decisions — execution model, system of record, and trust boundary — were all deferred, and it implicitly reads as "build all six services at once."

**What prompted this.** A collaborative brainstorming/review session to (a) critically review the brief, (b) fold it into the broader "Hermes Agent" personal-assistant vision, and (c) resolve the foundational forks before implementation. This plan captures the resolved design.

**Intended outcome.** A **lightweight, self-hosted, cloud-assisted v1** that runs in **shadow mode** (zero outbound actions): it ingests email events, classifies them cheaply and locally, builds durable **grounded, bitemporal state**, extracts structured **work objects**, records a full audit trail, and renders a **daily digest over Telegram**. This v1 is safe, fully testable, builds the golden set that unlocks later autonomy, and lays a provider-agnostic, graph-ready substrate that later fast-follows (attachments → meetings → other channels) and eventual on-box autonomy extend without a rewrite.

**Guiding principle (from the brief, endorsed):** *Convert incoming events into durable structured state once, then operate on that state.* Prefer events over scans, deterministic rules before model calls, cheap models before expensive ones, and cached grounded state before raw re-reads.

---

## Resolved decisions (the forks we closed)

| Decision | Choice | Rationale |
|---|---|---|
| v1 autonomy | **Shadow mode, zero actions** — but scaffold policy/audit/reversibility as real schema + enforced no-op checkpoints, autonomy pinned at level 0 | Safe, testable, builds golden set; turning on autonomy later is config, not rewrite |
| Model access | **Router abstraction, multi-backend** | Preserves local-first future; cheap/strong tiers can differ per provider |
| Cheap/bulk tier | **Local model on the box (Ollama)**; hosted strong tier only on escalation | Most email never leaves the box (privacy), zero per-token cost, proves low-intelligence-local goal day one |
| Orchestration | **n8n = transport only**; core logic in a testable Python service; **Postgres** authoritative store | Keeps state machine + policy in versioned, tested code — not GUI |
| State format | **Bitemporal, graph-projectable schema in Postgres v1** (entities, edges, valid-time + transaction-time, provenance) | Migration to Graphiti/Neo4j later is a projection, not a redesign; keeps extraction cost/portability under our own router |
| All state mutations | Flow through a **deterministic, validated mutation layer** — never raw LLM free-form writes | Doubles as injection defense; seed of the future "deterministic graph-language compiler" |
| Account topology | **Single personal Google account**, architected for multi-inbox + non-Google + self-hosted-mail via the adapter layer | Simplest trust boundary now; provider swap is a new adapter later |
| Core runtime | **Python** | Aligns Graphiti + AI/embeddings/eval ecosystem; n8n stays Node (transport) |
| Digest surface | **Telegram** now → scheduled morning/evening → voice message → voice call later | Easy to integrate, fits Hermes Agent; avoids re-using the inbox |
| Hosting | **Self-hosted box (home server / VPS)** | Closest to privacy/local-first trajectory; state + secrets on own hardware |
| Eval feedback | **In-flow corrections + periodic held-out labeled sample** | Continuous signal *and* stable unbiased metrics |

---

## Critical findings from the review (design constraints, not optional)

These are the gaps in the brief this architecture must close. Severity in brackets.

- **[C] The trust boundary is the whole system.** This ingests fully attacker-controlled content and (eventually) takes outbound actions — the canonical prompt-injection → tool-misuse → exfiltration shape. Injection defense must be **architectural** (content is data, never instructions; mutations are deterministic and validated), not detection-based.
- **[C] Model-emitted `confidence`/`sender_importance` must never gate consequential actions.** Injected text can forge high confidence and low sensitivity. Autonomy/escalation key off **trusted signals computed outside the model** — verified sender (SPF/DKIM/DMARC), known-contact list, deterministically-parsed amounts, action reversibility.
- **[C] "Summarize once" inherits summary errors forever.** Every extracted fact carries **provenance (source message id + span) + volatility/TTL**; `needs_full_thread_read` is triggered by defined events (new participant, detected contradiction, high-consequence action pending, summary age > TTL). Extraction is grounded and re-verifiable, not free prose.
- **[C] Local-model portability must be drawn per stage.** Triage / classification / entity extraction / dedup-blocking = local-capable now; grounded external replies / ambiguity resolution / outbound-action validation = strong model (and often human). Behind the router the line moves as local models improve. (See portability table below.)
- **[H] Two different dedup problems.** *Event idempotency* (same push twice → same id) is easy; *semantic work-object dedup* (same commitment across email/transcript/chat) is hard (embeddings + blocking + human-review threshold + reversible merges). Separate subsystems.
- **[H] Denial-of-wallet.** Anyone who can message you can trigger expensive reasoning → per-sender/per-day cost ceilings + cheap-path default + hard global cap.
- **[H] Backfill/cold-start is a distinct high-risk job** (mass mislabeling). v1 starts autonomy at zero and processes a bounded backlog in a labeled dry-run.
- **[H] Out-of-order/causal events** (`email.replied` before `email.received`): the work-object state machine must tolerate them (buffer + reconcile).
- **[M] Evals need an adversarial suite**, not just accuracy — unsafe-send prevention is a red-team/prompt-injection suite with a golden set, gated in CI before any autonomy ships.
- **[M] Degradation UX**: define what the digest shows when the store is corrupt or a model is down.
- **[M] Kill-switch & principal**: the unattended process has a bounded identity; the kill-switch halts *outbound* (relevant once past shadow mode), and pauses ingestion/spend.

---

## Architecture

### Layering (what runs where)

```
 Google (Gmail)                         [cloud, v1]
     │  push (Pub/Sub via users.watch) or poll (history.list)
     ▼
 n8n  ── transport only ──► normalizes to Common Event Envelope, POSTs to core
     ▼
 ┌───────────────── Core service (Python, on the box) ─────────────────┐
 │  ingestion → deterministic triage → cheap LOCAL classifier          │
 │      → escalation router → grounded extraction → validated mutation │
 │      → work-object state machine → audit                            │
 │  (policy engine + reversibility map present but pinned at AUTONOMY_0)│
 └─────────────────────────────────────────────────────────────────────┘
     │                         │                        │
     ▼                         ▼                        ▼
 Postgres (bitemporal      Model Router          Telegram digest
 state + audit + evals)   (local Ollama /        (morning + evening)
                           hosted strong)
```

### Component decomposition (representative paths, `services/` illustrative from the brief)

- `adapters/` — provider integrations behind a **Common Event Envelope** (channel-agnostic). v1: `adapters/gmail/`. The envelope is designed now so `adapters/whatsapp/`, `adapters/imessage/`, `adapters/imap/` (self-hosted mail) are additive. **The core imports nothing Gmail-specific.**
- `core/ingestion/` — envelope validation, stable event IDs, idempotency keys, dead-letter, replay, out-of-order buffering.
- `core/triage/` — deterministic, no-model rules (sender/domain, list headers, receipts, calendar notices, unsubscribe, thread state).
- `core/classify/` — cheap **local** model (Ollama) → structured `{requires_action, action_owner, action_type, urgency, response_required, category, model_confidence}`. Confidence here is **advisory only**.
- `core/router/` — model-routing abstraction; deterministic → local-cheap → hosted-strong on escalation, with per-stage model selection and cost accounting.
- `core/extraction/` — grounded fact/work-object extraction; **every field carries source message id + span + confidence**.
- `core/state/` — bitemporal thread/entity/edge store; incremental summary update; `needs_full_thread_read` triggers; TTL/volatility.
- `core/mutation/` — **the only writer.** Accepts a constrained, validated mutation intent (typed, schema-checked); rejects anything malformed. This is the deterministic gate that later becomes the graph-language compiler.
- `core/workobjects/` — typed work objects (Task, WaitingItem, Commitment, Question, Decision, Approval, Request, …) as state machines tolerant of out-of-order events.
- `core/dedup/` — event idempotency (v1) + semantic dedup seams (fast-follow: embeddings/pgvector + blocking + human-review threshold).
- `core/policy/` — autonomy levels (0–4) + reversibility map + trusted-signal inputs. **Present, enforced, pinned at 0.**
- `core/audit/` — append-only audit of every event, model call (with token counts + cost), classification, extraction, mutation, and (later) action/approval.
- `digest/` — renders grounded state into the Telegram morning/evening digest with drill-down to provenance; feedback capture (confirm/relabel) writes to the eval store.
- `shared/schemas`, `shared/events`, `shared/logging`, `shared/auth`, `shared/observability`.
- `storage/migrations`, `storage/repositories`.
- `tests/{unit,integration,evals,adversarial}`.

### The Common Event Envelope (channel-agnostic — designed once, now)

Every source normalizes to one shape so the core is provider-blind and future channels are additive:

```json
{
  "event_id": "stable-dedup-key",
  "source": "gmail",
  "channel": "email",
  "type": "message.received",
  "occurred_at": "…", "ingested_at": "…",
  "account_ref": "personal",
  "sender": { "raw": "…", "verified": {"spf": true, "dkim": true, "dmarc": "pass"}, "known_contact": true },
  "payload_ref": "pointer to raw content (NOT inlined into prompts as instructions)",
  "provenance": { "source_system_id": "gmail_msg_id", "thread_ref": "…" }
}
```

`sender.verified` and `known_contact` are **trusted signals** (computed deterministically); the model never overrides them.

### State: bitemporal & graph-projectable (Postgres now, Graphiti/Neo4j later)

- Core tables map to the brief's data domains (`people, organizations, conversations, messages, documents, meetings, tasks, requests, commitments, waiting_items, decisions, approvals, projects, events, actions, policies, model_runs, audit_entries`).
- Every **fact/edge** row carries: `subject`, `predicate`, `object`, **`valid_from`/`valid_to`** (when true in the world), **`recorded_at`/`invalidated_at`** (when we knew it — transaction time), `source_event_id`, `source_span`, `confidence`, `source_system`.
- Superseded facts are **invalidated, not deleted** (append-only), enabling point-in-time belief reconstruction — the exact shape a Graphiti/Neo4j temporal KG expects, so the later migration is a projection.

---

## v1 scope & fast-follow roadmap

**v1 (email only):** `ingest → deterministic triage → local classify → escalate-if-needed → grounded extraction → validated mutation → bitemporal state + work objects → audit → Telegram digest`, plus in-flow corrections + held-out labeled sample. **Zero outbound actions.**

**Fast-follow 1 — Attachments & documents.** Hash → dedup → **deterministic security gate on every URL and file *before any LLM sees it*** (type/size allow-list, malware/AV scan, link defusing/sandboxed resolution, password-file handling) → classify → store original → extract text/metadata → summarize → index (pgvector) → link to thread/work object. *The security gate is a hard prerequisite, per requirement.*

**Fast-follow 2 — Meeting output (provider-agnostic).** Evaluate existing transcript integrations vs. build; ingest transcript/notes → grounded extraction of decisions/commitments/actions → **semantic dedup** against existing work objects (provenance-merge, not duplicate).

**Fast-follow 3 — Other channels.** WhatsApp, iMessage, Instagram, Facebook Messenger as **new adapters onto the same envelope**, each with channel-specialized normalization; core logic unchanged.

**Later (gated on eval + audit maturity):** autonomy levels 1→4, knowledge-grounded replies, follow-up/waiting engine, sender-aware routing; migration of state into Graphiti/Neo4j with the deterministic graph-language compiler; digest evolution to voice message → voice call; self-hosted mail/files/calendar adapters replacing Google.

---

## Local-model portability boundary (drawn per stage)

| Stage | v1 execution | Portable to low-intelligence local? |
|---|---|---|
| Deterministic triage | rules, no model | N/A (already local) |
| Bulk classification | **local (Ollama)** | Yes — target |
| Entity/fact extraction | local, escalate on low signal | Yes, with grounded/constrained output |
| Dedup blocking | deterministic + embeddings (fast-follow) | Yes |
| Ambiguity resolution | **hosted strong** on escalation | Partial — moves local over time |
| Grounded external reply / outbound validation | **hosted strong** (+ human) | Not yet |

The router makes this line configurable per stage so it shifts as local models improve — without touching call sites.

---

## Security model (highest-stakes; built in from v1)

- **Content is data, never instructions.** Raw email bodies/attachments enter prompts inside clearly delimited, untrusted-content framing; system/instruction context is separated. The mutation layer is the only writer and rejects anything not matching the typed schema.
- **Trusted vs. model-asserted signals** are separated in schema; only trusted signals gate consequential logic (pinned off in v1, enforced from the start).
- **Deterministic pre-LLM gate for links/files** (fast-follow 1, mandatory).
- **Secrets on the box**: OAuth tokens/credentials encrypted at rest (e.g., age/SOPS or OS keychain), least-privilege Google scopes (v1 read-only: `gmail.readonly`), no secrets in git (existing `.gitignore`/`.forceignore` patterns extend).
- **Denial-of-wallet**: per-sender/day + global hard cost ceilings, cheap-path default, alert on breach.
- **Kill-switch & bounded principal** for the unattended process; halts outbound + pauses ingestion/spend (matters once past shadow mode, wired now).
- **Data-at-rest**: the local mirror is a second copy of sensitive mail → encrypted volume; retention + deletion path defined (delete a thread → invalidate its facts + purge raw payload).

---

## Token / cost efficiency

- Deterministic → local-cheap → hosted-strong hierarchy; **most messages resolve before any hosted call**.
- Cached grounded state (thread summaries, sender classifications, extracted entities) avoids recomputation; `needs_full_thread_read` gates raw re-reads.
- Every model call logged with prompt/completion token counts + cost in `model_runs`; cost dashboards per workflow; regression alarms if cost-optimization degrades quality.

---

## Testing & evaluation strategy

- **Unit**: deterministic triage rules, envelope validation, idempotency, state-machine transitions (incl. out-of-order), mutation-layer rejection of malformed/injected intents.
- **Integration**: end-to-end email → digest against recorded fixtures; replay & dead-letter; degradation paths (store/model down).
- **Evals (golden set)**: action detection, task/deadline/owner extraction, thread-summary fidelity (grounded-to-span), dedup, classification accuracy — measured on the **held-out labeled sample**; in-flow corrections feed regression fixtures.
- **Adversarial suite**: prompt-injection corpora embedded in bodies/subjects/attachments; assert (a) no unintended mutation, (b) trusted signals never overridden, (c) unsafe-send prevention (pre-wired for when autonomy turns on). **CI gate before any autonomy ships.**
- **Test seams**: injectable `ModelClient` and `ToolClient` so all model/tool calls are mockable and deterministic in tests.

---

## Verification (how we prove v1 works end-to-end)

1. **Adapter/envelope**: feed recorded Gmail push/history payloads through `adapters/gmail`; assert well-formed envelopes with correct trusted-signal computation (SPF/DKIM/DMARC, known-contact).
2. **Ingestion idempotency**: replay the same event twice → exactly one processed record; out-of-order `replied`/`received` → correct final work-object state.
3. **Triage/classify/route**: fixtures covering deterministic-only, local-cheap, and escalation paths; assert routing + advisory-only confidence handling; confirm hosted calls happen only on escalation.
4. **Grounded extraction & mutation**: assert every extracted fact has a resolvable source span; feed a prompt-injection fixture and assert the mutation layer rejects/ignores injected instructions.
5. **State & digest**: run a labeled backlog in dry-run; render the Telegram digest (decisions / waiting / completed-would-have / FYI) with working drill-down to provenance; verify zero outbound actions occurred (audit shows observe-only).
6. **Evals & cost**: run the golden-set eval → baseline metrics recorded; run the adversarial suite → all pass; confirm `model_runs` captured token counts + cost and that per-sender/global ceilings trip in a synthetic flood test.
7. **Feedback loop**: submit an in-flow correction from the digest → verify it lands in the eval store and appears as a regression fixture.

---

## Open items to resolve during implementation (non-blocking)

- Gmail ingestion mechanism on a self-hosted box: **polling via `history.list`** (simplest, no public endpoint) vs **push (Pub/Sub + Cloudflare/ngrok tunnel)** — recommend polling for v1, push-ready envelope. Confirm during build.
- Concrete local model for the cheap tier (e.g., a small Llama/Qwen/Mistral in Ollama) sized to the box's RAM/GPU.
- Exact held-out sample size and labeling cadence for stable metrics.
- Secrets mechanism final choice (age/SOPS vs OS keychain) for the box.
- Whether the existing `hermes-agent-utilities/models-api-client` (Salesforce Models API) registers as one hosted backend behind the router.
