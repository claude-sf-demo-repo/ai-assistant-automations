# Security & Trust Boundary

_Epic: E7 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Define the cross-cutting security controls that make the system safe to run against fully attacker-controlled content. This subsystem ingests hostile input from day one and has two live egress surfaces (Telegram digest, off-box escalation); shadow mode is **low-egress, not zero-risk**. This spec is a **control catalogue**: most controls are enforced inside other subsystems (mutation layer, router, digest, ingestion) and are referenced here, plus the controls that live nowhere else (trust model, kill-switch, denial-of-wallet caps, secrets, erasure, audit chain).

## Scope

**In v1**
- The trust model: domain-authentication vs identity-trust split; `known_contact` semantics; thread-membership by server id; confusable-domain detection.
- `content-is-data-never-instructions` framing enforced on bodies **and** metadata.
- Per-object autonomy gating on provenance + trust-age (autonomy pinned at 0).
- Component isolation and network posture (poll, no inbound endpoint).
- Secrets management; denial-of-wallet caps; authenticated kill-switch; hash-chained audit; GDPR subject-level erasure; supply-chain pinning/SBOM.

**Deferred / out of scope**
- Any autonomy level > 0 (gated on a state-store red-team, not just classifier evals).
- Attachment/URL security gate (fast-follow 1) — mandatory before documents are ingested.
- Push ingestion (only if ever adopted; would require Google OIDC verification).

## Interfaces & data contracts

**Trust signals (computed outside the model; the model may never override them):**
```python
class DomainAuth(BaseModel):     # proves domain authentication, NOT identity trust
    spf: bool
    dkim: bool
    dmarc: Literal["pass", "fail", "none"]

class TrustView(BaseModel):
    sender_address: str          # identity is the verified address only
    domain_authenticated: bool   # derived from DomainAuth
    identity_trusted: bool        # user-curated allowlist lookup ONLY (never auto-learned)
    known_contact: bool           # non-transitive; gates nothing consequential
    confusable_of: str | None     # set if display/domain is a homoglyph/cousin of an allowlisted one
    thread_trust_reset: bool      # True if a new participant joined the server thread
```

**Allowlist (identity trust) is human-curated and versioned; never mutated by the ingestion path** — it is a privileged mutation (spec 02).

**Autonomy / trust-age gate:**
```python
class AutonomyDecision(BaseModel):
    object_id: UUID
    autonomy_level: int          # v1: always 0
    sender_trusted_at_creation: bool
    trust_age_days: float
    allowed: bool                # v1: consequential actions always False
# Objects created while a sender was untrusted never retroactively gain authority.
```

**Kill-switch (authenticated, remotely reachable):**
```python
def kill_switch(command_chat_id: int) -> None:
    # command_chat_id must equal the authorized chat-id.
    # Halts: off-box escalation, digest send, and ingestion. Idempotent; audited.
```

**Denial-of-wallet caps (config, pydantic-settings):**
```
LOCAL_INFERENCE_QUEUE_MAXSIZE     # backpressure threshold
PER_SOURCE_FAIR_SHARE             # per-sender bounded queue share
GLOBAL_SPEND_CAP                  # on breach: fail safe-and-degrade → deterministic-only + flag
```

**GDPR erasure contract:**
```python
def erase_subject(subject_ref: str) -> ErasureReport:
    # Purges across ALL stores: facts/edges, work objects, raw payloads, embeddings,
    # audit, model_runs, eval fixtures. Emits a per-store report. Prefer refs+hashes
    # so little raw content exists to purge.
```

**Audit chain:** each `audit_entries` row stores `prev_hash` and `entry_hash = H(prev_hash || canonical(entry))`, forming a tamper-evident hash chain (append-only).

## Invariants

1. Identity trust is read only from the human-curated allowlist; it is never auto-learned or model-asserted.
2. Domain authentication is never treated as identity trust (an attacker's own domain passes SPF/DKIM/DMARC).
3. `known_contact` is non-transitive and gates nothing consequential.
4. All email content **and metadata** (subject, display name, filenames, headers) is treated as untrusted data, never instructions.
5. No inbound network endpoint exists in v1 (polling only).
6. No secret is ever written to git (public repo); secrets live in the store only.
7. The kill-switch, when tripped, halts escalation, digest, and ingestion, and only the authorized chat-id can trip it.
8. Consequential/privileged mutations can never originate from the ingestion path (enforced by spec 02).
9. The audit chain is append-only and verifiable; erasure is recorded, not achieved by unlogged deletion.

## Failure modes

- **Confusable/cousin domain detected** → treat as untrusted, flag in digest, never identity-trust.
- **New participant joins a thread** → reset thread trust; re-evaluate downstream gating.
- **Spend cap breached** → degrade to deterministic-only processing and flag the digest; never silently overspend.
- **Secret missing at boot** → fail closed (refuse to start the affected process), never fall back to an insecure default.
- **Audit chain break detected** → alert; refuse to advance autonomy.

## Acceptance criteria

- [ ] `TrustView` separates domain-authentication from identity-trust; a test proves a fully SPF/DKIM/DMARC-passing attacker domain is not identity-trusted.
- [ ] Identity-trust changes are rejected from the ingestion path and only accepted via the privileged/human path.
- [ ] `known_contact` gates no consequential logic (asserted by test).
- [ ] Thread membership is computed from server thread-id; a forged `References` header does not confer membership.
- [ ] Confusable-domain detection flags a homoglyph/cousin of an allowlisted domain.
- [ ] A new participant resets thread trust.
- [ ] Autonomy is pinned at 0; objects created under untrusted senders cannot gain authority by trust-age alone.
- [ ] No inbound endpoint is exposed; ingestion is poll-only.
- [ ] `git` history and working tree contain no secrets (CI secret-scan passes); `.env` is gitignored and only `.env.example` is tracked.
- [ ] Denial-of-wallet: synthetic flood triggers per-source fairness + backpressure + fail-safe-and-degrade at the global cap.
- [ ] Kill-switch from the authorized chat-id halts escalation + digest + ingestion; from any other chat-id it is ignored.
- [ ] `erase_subject` purges the subject across all listed stores and returns a per-store report.
- [ ] Audit entries form a verifiable hash chain; a mutation to any past entry is detectable.

## Test seams

- `TrustResolver` is pure over envelope + allowlist snapshot — table-driven tests.
- `Clock` injected for trust-age computation.
- `SecretsProvider` injectable (fake in tests; age/SOPS or OS keychain in prod).
- Adversarial suite (spec 08) exercises injection in bodies/subjects/display-names/filenames and asserts trusted signals are never overridden and escalation never leaks cross-thread content.
- Kill-switch and cap behaviors tested via injected queue/cost meters.

## Reviewer-finding traceability

| Finding (plan) | How this spec addresses it |
|---|---|
| `[C · Security] Trusted-signal boundary is spoofable/self-poisoning` | Domain-auth vs identity-trust split; allowlist-only trust; non-transitive `known_contact`; server-thread-id membership; confusable detection; new-participant reset. |
| `[C · Security] Mutation layer validates shape, not authority` | Privileged-mutation class incl. identity/trust changes barred from ingestion (enforced in spec 02, referenced here). |
| `[C · Security] Attacker controls what escalates` | Sensitive→HOLD, minimized/redacted off-box payloads (enforced in spec 03); kill-switch halts escalation. |
| `[H · Security] Autonomy flips on a poisoned store` | Per-object trust-age gating; "red-team the state store before flipping" as a gate. |
| `[H · Security] Unified KG cross-context leak` | Account-scoped identity/trust; `account_ref` everywhere (schema in spec 00/02). |
| `[H · Security] Endpoint/component blast radius` | Poll-only, component isolation, least-privilege users/containers, minimize process reach. |
| `[M, folded] Metadata is an injection site` | Untrusted framing on subject/display-name/filenames/headers; identity by verified address only. |
| `[M, folded] Denial-of-wallet / Kill-switch / Privacy / Supply chain` | Per-source-fair queues + backpressure + fail-safe cap; authenticated remote kill-switch; subject-level erasure across all stores; pinning/SBOM. |
| `[M, folded] Hash-chain audit` | Tamper-evident append-only audit chain. |

## Open items

- Secrets mechanism: age/SOPS vs OS keychain (box-dependent).
- Confusable/cousin-domain detection library/approach.
- Concrete state-store red-team methodology to gate the eventual autonomy flip.
- Component isolation implementation (separate OS users vs containers) on the target box.
