# Classification & Extraction

_Epic: E4 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Assign each message a **coarse** label using thread-aware local inference, route on
self-consistency (not the miscalibrated self-reported scalar), and produce **grounded**
extractions where extractive fields are byte-verifiable functions of a cited span and dates/owners
are computed by **deterministic normalizers** — never by the LLM. An explicit **UNCERTAIN**
abstention route feeds both the digest and the cold-start labeling stream.

## Scope

**In v1**
- Coarse taxonomy: `action | no-action | FYI | approval | UNCERTAIN` (coarse only).
- Thread-conditioned inference; unit of record = per-message.
- Self-consistency routing (k=3–5 samples; label agreement/entropy); per-field confidence.
- UNCERTAIN abstention route (budgeted, ranked by trusted-signal risk).
- Grounded extraction: extractive (span byte-verifiable) vs abstractive (entailment-checked).
- Deterministic date normalizer (timezone + business-day + `date_precision`).
- Owner resolution by coreference over the typed participant list; `owner_ambiguous` → UNCERTAIN.
- Calibration measured via ECE on the seed set.

**Deferred / out of scope**
- Fine-grained action typing (escalated or deferred).
- Abstractive summary generation for high-consequence threads beyond escalation.

## Interfaces & data contracts

```python
CoarseLabel = Literal["action","no-action","FYI","approval","UNCERTAIN"]

@dataclass(frozen=True)
class ClassifyInput:
    message: Message                     # the unit of record
    thread_summary: str | None           # running summary (conditioning)
    open_work_objects: list[WorkObjectRef]

@dataclass(frozen=True)
class ClassifyResult:
    label: CoarseLabel
    per_field_confidence: dict[str, float]   # from self-consistency, NOT raw scalar
    self_consistency: SelfConsistency        # agreement/entropy over k samples
    route: Literal["accept-local","escalate","hold","uncertain"]
```

### Self-consistency routing

```python
def classify(inp, model: ModelClient, k: int = 5) -> ClassifyResult:
    samples = [model.generate(req_with(inp)) for _ in range(k)]  # temp>0 for diversity
    agreement, entropy = tally(samples)
    conf = per_field_confidence(samples)          # empirical agreement, not model's self-report
    # routing decision defers to Router (spec 03); this returns the signals
```

The model's **self-reported** confidence is miscalibrated and is **not** used for routing.
Routing keys on empirical **label agreement/entropy** across k samples.

### Grounded extraction

```python
@dataclass(frozen=True)
class ExtractedField:
    name: str
    kind: Literal["extractive","abstractive"]
    value: str
    source_span: Span                      # {message_id,start,end}
    relative_expression: str | None        # e.g. "next Friday" — LLM emits this, not a date

@dataclass(frozen=True)
class NormalizedDate:
    resolved: date | None
    date_precision: Literal["exact","day","week","month","ambiguous"]
```

- Extractive field values must be a **deterministic function of their cited span** (byte-verifiable
  at the mutation layer, spec 02). Abstractive fields are **entailment-checked**.
- The LLM emits the **span + relative expression only**. A deterministic normalizer computes the
  absolute date (timezone-aware, business-day-aware, sets `date_precision`). **The LLM never does
  date arithmetic.**
- Owner is resolved by **coreference against the typed participant list** (from the envelope,
  spec 00). If unresolved → `owner_ambiguous` → the field routes to UNCERTAIN.

### Date normalizer

```python
def normalize_date(expr: str, *, ref: datetime, tz: str, clock: Clock) -> NormalizedDate: ...
```

Pure over `(expr, ref, tz)`; `Clock` supplies `ref` in production. Deterministic and unit-tested
against a fixture table.

## Invariants

1. Labels are drawn only from the coarse taxonomy; there is always a valid UNCERTAIN option.
2. Routing uses empirical self-consistency, never the model's self-reported confidence.
3. Every extractive field carries a resolvable, supporting span; the value derives from the span.
4. Absolute dates are produced only by the deterministic normalizer, never emitted by the LLM.
5. An unresolved owner yields `owner_ambiguous` and routes to UNCERTAIN — never a guessed owner.
6. Confidence is represented per-field, not as a single message scalar.

## Failure modes

- **k-sample disagreement** → escalate (spec 03) or UNCERTAIN.
- **Ambiguous relative date** → `date_precision="ambiguous"`; not turned into a false exact date.
- **Ambiguous owner** → UNCERTAIN.
- **Span doesn't support the value** → rejected downstream at the mutation layer (spec 02).

## Acceptance criteria

- [ ] Classifier returns a coarse label plus per-field confidence derived from k-sample agreement, not the model's self-report.
- [ ] High k-sample disagreement routes to escalate/UNCERTAIN (fixture-driven).
- [ ] Thread conditioning changes the label for a message like "sounds good, go ahead" given prior thread state.
- [ ] Every extractive field emits a span; a value not present in its span is flagged (and rejected by spec 02).
- [ ] The LLM output contains a relative expression, and the absolute date is produced only by `normalize_date`.
- [ ] `normalize_date` is deterministic and passes a fixture table (timezone + business-day + precision cases).
- [ ] An unresolved owner yields `owner_ambiguous` → UNCERTAIN, never a guessed owner.
- [ ] UNCERTAIN items are produced and are ranked by trusted-signal risk for the budgeted digest section.
- [ ] ECE is computed on the seed set and reported (spec 08).

## Test seams

- Fake `ModelClient` scripts k-sample agreement/disagreement.
- `normalize_date` is a pure unit with a fixture table; `Clock` injected for `ref`.
- Coreference tested against typed participant-list fixtures.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[H · AI]` Self-reported confidence miscalibrated | route on self-consistency (k-sample agreement/entropy); ECE on seed set |
| `[H · AI]` No abstention path | explicit UNCERTAIN route, budgeted, risk-ranked |
| `[H · AI]` Deadline/owner extraction fails on small models | LLM emits span+expression only; deterministic date normalizer; owner coreference; ambiguous→UNCERTAIN |
| `[H · AI]` Classification needs thread conditioning | thread-conditioned inference; per-message unit of record |
| `[C · Sec+AI]` Span-verification | extractive fields carry byte-verifiable spans; abstractive entailment-checked |

## Open items

- k value (3 vs 5) tuned against latency/cost on the chosen local model.
- Date normalizer library (dateparser / duckling-class).
- Coreference approach for owner resolution (rules vs light model).
