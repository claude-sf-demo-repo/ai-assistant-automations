# Model Router & Local Inference

_Epic: E3 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Route inference across a local cheap tier (Ollama) and a hosted strong tier, escalating only on
model *uncertainty*. Cleanly split into `ModelClient` (per-backend transport), a pure `Router`
(stage→backend policy + escalation predicate), and `CostMeter` (accounting). Enforce that
escalation payloads are minimized/redacted and that sensitive content is held for a human, never
auto-sent off-box.

## Scope

**In v1**
- `ModelClient` transports: local Ollama backend + one hosted strong backend.
- Pure `Router`: explicit escalation predicate, escalation-rate SLO (~≤15%), fail-closed on cap-hit.
- `CostMeter`: per-call tokens + cost + prompt-hash + model tag + sampling params → `model_runs`.
- Escalation payload minimization + PII redaction; sensitive → HOLD; off-box payload logging.
- Denial-of-wallet: per-source-fair bounded queues + backpressure; global cap fails safe-and-degrade.

**Deferred / out of scope**
- Additional hosted backends beyond one.
- On-box fine-tuning; multi-GPU scheduling.

## Interfaces & data contracts

```python
class ModelClient(Protocol):
    backend: str                                   # "ollama-local" | "hosted-strong"
    def generate(self, req: ModelRequest) -> ModelResponse: ...

@dataclass(frozen=True)
class ModelRequest:
    stage: str                                     # "classify" | "extract" | "entail" ...
    prompt_template_id: str; prompt_hash: str
    inputs: dict; k_samples: int = 1
    temperature: float = 0.0; seed: int | None = None

@dataclass(frozen=True)
class RouteDecision:
    backend: str                                   # chosen transport
    reason: str                                    # "cheap-default" | "uncertain-escalate" | "hold-for-human" | "cap-fail-closed"

class Router:                                      # PURE — no I/O
    def choose(self, stage: str, signals: RoutingSignals) -> RouteDecision: ...
```

### Routing vs. gating (critical distinction)

- **Routing to a bigger model** is triggered by **model uncertainty** — there is no adversarial
  payoff in forging *low* confidence, so this is safe to key on model signals.
- **Gating an action** must use **trusted signals** (spec 07) and is **not** the router's job.
  The router routes inference; it never authorizes a consequential action.

### Escalation predicate

```python
def should_escalate(signals) -> bool:
    if signals.sensitive:            return False   # → HOLD for human, NOT off-box
    if signals.self_consistency_disagreement:  return True
    if signals.per_field_low_confidence:        return True
    return False
```

- `sensitive` content is a **hold-for-human** branch — never auto-escalated off-box (escalation =
  content leaving the box; "sensitive → escalate" would leak the most sensitive mail most).
- Escalation-rate SLO ~**≤15%**, monitored as a first-class cost metric.
- On **cap-hit**, the router **fails closed** to `UNCERTAIN` (surfaced in the digest), never
  silently downgrades to a guess.

### Escalation payload minimization

```python
def build_escalation_payload(item) -> dict:
    # minimum necessary; PII-redacted; NEVER auto-attach cross-thread or PII state
    return redact_pii(minimize(item.only_needed_spans))
```

Every off-box payload is logged (what left the box, when, to which backend).

### CostMeter → `model_runs`

```sql
CREATE TABLE model_runs (
  id uuid PRIMARY KEY, stage text, backend text,
  prompt_template_id text, prompt_hash text,
  model_tag text, temperature real, seed bigint, k_samples int,
  prompt_tokens int, completion_tokens int, cost_usd numeric,
  account_ref text, source_event_id uuid, ran_at timestamptz DEFAULT now()
);
```

### Denial-of-wallet

Per-source-fair bounded queues feed the local-inference stage with backpressure; a global spend
cap **fails safe-and-degrade** (deterministic-only classification + a low-trust flag on the
digest), never fails open into unbounded hosted calls.

## Invariants

1. `Router.choose` is pure (no network/DB/clock); all I/O is in `ModelClient`/`CostMeter`.
2. Sensitive content never leaves the box automatically (HOLD branch).
3. Every off-box call is preceded by minimization + PII redaction and is logged.
4. Cap-hit fails closed to UNCERTAIN.
5. Every model call writes a `model_runs` row with prompt-hash + model tag + sampling params.
6. The router never authorizes an action; it only selects a backend.

## Failure modes

- **Local backend down** → escalate per predicate; if hosted also unavailable → UNCERTAIN + degrade.
- **Global cap reached** → deterministic-only + low-trust flag (safe-and-degrade).
- **Redaction failure** → do not send; route to HOLD.
- **SLO breach (escalation rate > cap)** → alert; backpressure tightens.

## Acceptance criteria

- [ ] `ModelClient`, `Router`, `CostMeter` are separate; `Router.choose` has no I/O and is unit-tested purely.
- [ ] Self-consistency disagreement or per-field low confidence routes to the hosted backend.
- [ ] `sensitive=True` routes to HOLD and never produces an off-box call (asserted).
- [ ] Cap-hit produces `RouteDecision(reason="cap-fail-closed")` → UNCERTAIN, never a downgraded guess.
- [ ] Every off-box payload is minimized, PII-redacted, and logged; a cross-thread/PII field is never auto-attached.
- [ ] Every model call writes a `model_runs` row with prompt-hash, model tag, temperature, seed, tokens, cost.
- [ ] Global spend cap trips deterministic-only degrade + low-trust flag in a synthetic flood test.
- [ ] Escalation rate is measured against the ~≤15% SLO and alerts on breach.

## Test seams

- Fake `ModelClient` scripts responses and self-consistency disagreement.
- `Router` tested as a pure function over `RoutingSignals` fixtures.
- Redaction has a unit suite; a "leak canary" fixture asserts no PII/cross-thread field ever appears in an off-box payload.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[C · AI]` Escalation function undefined; conflates routing & gating | explicit `should_escalate` predicate; routing≠gating; SLO; fail-closed on cap |
| `[C · Security]` Attacker controls what escalates → exfiltration | sensitive→HOLD; minimized/PII-redacted payloads; off-box logging |
| `[M, folded]` Denial-of-wallet | per-source-fair bounded queues + backpressure; global cap safe-and-degrade |
| `[L, folded]` Reproducibility | prompt-hash + model tag + sampling params in `model_runs`; temp 0 / fixed seed |

## Open items

- Whether `hermes-agent-utilities/models-api-client` (Salesforce Models API) registers as the hosted backend.
- Local model choice/size (30B-class MoE vs 7–8B) — drives the escalation rate.
- PII redaction library/approach.
