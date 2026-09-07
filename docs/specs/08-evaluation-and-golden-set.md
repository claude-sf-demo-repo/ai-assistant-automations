# Evaluation & Golden Set

_Epic: E8 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Measure whether the system is safe and correct **without being blind to the misses**. The dominant v1 harm is silent state corruption and a confident-but-wrong digest, so the gating metrics are **actionable-class recall** (are we dropping things that mattered?) and **faithfulness** (is every asserted claim entailed by its cited span?). In-flow corrections are selection-biased and are used only as regression fixtures — never as an unbiased metric.

## Scope

**In v1**
- Seed golden set (~300–500 threads) hand-labeled from the backlog dry-run, stratified, self-consistency spot-checked.
- Recall-first metrics: actionable-class recall, faithfulness (gating), calibration (ECE).
- Held-out sample = stratified random draw of **all** inbound, incl. no-action/FYI; audit-sample of the dropped/FYI bucket to estimate miss rate.
- The trust-bar gate that flags the digest low-trust when unmet.
- Adversarial suite as a CI gate before any autonomy.
- Golden-set poisoning defenses; per-field labeling guidelines.

**Deferred / out of scope**
- Full-depth held-out labeling program (design present; depth deferred per plan).
- Autonomy-readiness evals beyond v1 (require state-store red-team, spec 07).
- Semantic-dedup evals (fast-follow).

## Interfaces & data contracts

```python
class GoldenLabel(BaseModel):
    thread_id: str
    message_id: str
    coarse_class: Literal["action", "no_action", "fyi", "approval", "uncertain"]
    fields: dict[str, Any]           # extractive/abstractive field truths + spans
    labeler_id: str
    labeled_at: datetime
    provenance: list[ProvenanceRef]
    immutable: bool = True           # held-out labels are immutable to in-flow edits

class EvalResult(BaseModel):
    dataset: Literal["seed", "held_out", "adversarial", "dropped_bucket_audit"]
    actionable_recall: float          # GATING
    actionable_precision: float
    faithfulness: float               # GATING: fraction of claims entailed by cited span
    ece: float                        # calibration error on the seed set
    estimated_miss_rate: float | None # from dropped/FYI bucket audit
    prompt_template_hash: str
    model_tag: str
    sampling_params: dict             # temperature 0 / fixed seed for reproducibility

class TrustBarGate(BaseModel):
    min_actionable_recall: float      # X
    min_faithfulness: float           # Y
    def passes(self, r: EvalResult) -> bool: ...
    # When not passed → digest.low_trust = True (spec 06).
```

**Faithfulness metric:** for each asserted field/claim, verify the value is entailed by its cited `source_span` — extractive fields by deterministic byte-match, abstractive by entailment check (spec 02/04). Faithfulness = fraction of claims that pass.

**Adversarial suite corpus:** injection payloads embedded in bodies, subjects, display names, and filenames. Assertions: (a) no unintended or mis-grounded mutation is produced; (b) trusted signals are never overridden; (c) the digest neutralizes the payload; (d) escalation never leaks cross-thread content.

## Invariants

1. The held-out sample is a stratified random draw of **all** inbound classes (incl. no-action/FYI) — never only surfaced items.
2. Actionable-class recall and faithfulness are the gating metrics; a release/autonomy step cannot pass without meeting them.
3. In-flow corrections are regression fixtures only; they never feed the unbiased metric computation.
4. Held-out labels are curated separately and immutable to in-flow edits.
5. A correction that would create or modify a **rule** requires human review before taking effect.
6. Evals run at temperature 0 / fixed seed and log prompt-template hash + model tag + sampling params.
7. Model-hitting eval/adversarial suites run separately from the inner-loop CI.

## Failure modes

- **Recall below X or faithfulness below Y** → gate fails; digest flagged low-trust; autonomy remains barred.
- **Dropped-bucket audit shows high miss rate** → escalate; re-tune triage/classification thresholds; never ignore.
- **Correction attempts to alter held-out labels** → rejected (immutability invariant).
- **Labeler disagreement on a field** → treat as aleatoric → route that field to UNCERTAIN; capture in guidelines.

## Acceptance criteria

- [ ] Seed golden set of ~300–500 stratified threads is labeled with per-field spans and self-consistency spot-checks.
- [ ] Held-out sampler draws stratified across all inbound classes, including no-action/FYI.
- [ ] Actionable-class recall and faithfulness are computed and enforced as gating metrics.
- [ ] ECE is computed on the seed set.
- [ ] A periodic audit-sample of the dropped/FYI bucket produces an estimated miss rate.
- [ ] In-flow corrections land as regression fixtures and are excluded from unbiased metric computation (asserted by test).
- [ ] Held-out labels are immutable to in-flow edits; rule-changing corrections require review.
- [ ] Trust-bar gate flips `digest.low_trust` when unmet.
- [ ] Adversarial suite runs as a CI gate and asserts no unintended/mis-grounded mutation, no trusted-signal override, digest neutralization, and no cross-thread escalation leak.
- [ ] Eval runs are reproducible (temp 0 / fixed seed) and log prompt hash + model tag + params.
- [ ] Eval/adversarial suites are separated from inner-loop CI.

## Test seams

- `Store` and `Clock` injected; datasets are fixtures.
- `ModelClient` faked for deterministic metric tests; real backends only in the separated model-hitting suite.
- Metric functions (recall, faithfulness, ECE) are pure and unit-tested against tiny hand-built fixtures.
- Adversarial corpus is a versioned fixture directory (synthetic only — no real personal data).

## Reviewer-finding traceability

| Finding (plan) | How this spec addresses it |
|---|---|
| `[C · AI] Eval loop is structurally blind to misses` | Recall-first; stratified full-stream held-out; dropped-bucket audit; corrections are fixtures only. |
| `[C · Security+AI] Span-verification / faithfulness gate` | Faithfulness as a gating metric = fraction of claims entailed by cited span. |
| `[H · AI] Miscalibrated confidence` | ECE measured on the seed set; drives self-consistency routing (spec 04). |
| `[H · AI] Cold-start` | Seed golden set (~300–500, stratified, spot-checked); trust-bar gate. |
| `[M, folded] Silent state corruption is the real v1 harm` | Faithfulness is a gating eval; corrections trigger invalidation cascade (spec 02). |
| `[M, folded] Golden-set poisoning` | Held-out set curated separately + immutable; rule-changing corrections reviewed; corrections human-authenticated + provenance-tagged. |
| `[L, folded] Reproducibility / labeling quality / suite separation` | Temp 0 + fixed seed + logged params; per-field guidelines + delayed re-label overlap; suites separated from inner-loop CI. |
| Adversarial as CI gate before autonomy | Adversarial suite defined as a gate. |

## Open items

- Concrete thresholds X (recall) and Y (faithfulness) for the trust-bar gate.
- Held-out sample size + labeling cadence.
- Per-field labeling guidelines document + delayed re-label overlap schedule.
- Entailment checker choice for abstractive faithfulness.
