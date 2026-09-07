# Work Objects

_Epic: E5 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Model the durable, actionable units the digest reports on — Task, WaitingItem, Commitment,
Question, Decision, Approval, Request — as typed state machines whose transitions are
**event-time-ordered and commutative**, so out-of-order events converge deterministically. Work
objects are created and transitioned **only** through the mutation layer (spec 02).

## Scope

**In v1**
- Typed work objects with per-type state machines.
- Event-time folding by `occurred_at` per aggregate (deterministic convergence; no timed buffer).
- Legal-transition invariant; idempotency-key uniqueness.
- Creation/transition exclusively via `UpsertWorkObject` / `TransitionWorkObject` (spec 02).

**Deferred / out of scope**
- Semantic dedup / cross-source merge (fast-follow — v1 has event idempotency only).
- Follow-up / waiting engine automation (later; observe-only in v1).

## Interfaces & data contracts

```python
WorkObjectType = Literal["Task","WaitingItem","Commitment","Question","Decision","Approval","Request"]

@dataclass(frozen=True)
class WorkObject:
    id: str; account_ref: str; wo_type: WorkObjectType
    idem_key: str                          # stable dedup key for event idempotency
    state: str
    fields: dict                           # typed per wo_type
    provenance: list[Span]
    created_at: datetime; updated_at: datetime
```

```sql
CREATE TABLE work_object (
  id uuid PRIMARY KEY, account_ref text NOT NULL, wo_type text NOT NULL,
  idem_key text NOT NULL, state text NOT NULL, fields jsonb NOT NULL,
  created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
  UNIQUE (account_ref, wo_type, idem_key)
);
```

### State machines (representative)

```
Task:        OPEN → IN_PROGRESS → DONE ; OPEN → CANCELLED
WaitingItem: WAITING → RECEIVED ; WAITING → EXPIRED
Approval:    PENDING → APPROVED ; PENDING → REJECTED
Request:     OPEN → FULFILLED ; OPEN → DECLINED
```

Each type declares its legal transitions; `TransitionWorkObject` (spec 02) enforces the
legal-transition invariant.

### Event-time folding (commutativity)

```python
def fold(events: list[Event]) -> WorkObject:
    for e in sorted(events, key=lambda e: e.occurred_at):   # event-time, not arrival
        apply_transition(e)
```

Folding is by `occurred_at`, so `email.replied` arriving before `email.received` still converges
to the correct final state. Transitions are designed commutative under event-time sorting — **not**
a timed reordering buffer.

## Invariants

1. Work objects are created/mutated only via the mutation layer's commands.
2. `(account_ref, wo_type, idem_key)` is unique; re-processing an event is a no-op.
3. Only legal transitions are applied; illegal transitions are rejected (spec 02).
4. Folding by `occurred_at` yields the same final state regardless of arrival order.
5. Every work object carries provenance spans back to source messages.

## Failure modes

- **Out-of-order events** → converge via `occurred_at` sort.
- **Duplicate event** → idempotency-key no-op.
- **Illegal transition attempt** → rejected by `TransitionWorkObject` invariant.
- **Conflicting concurrent updates for a thread** → serialized by the per-thread advisory lock (spec 01).

## Acceptance criteria

- [ ] Each work-object type declares a state machine with explicit legal transitions.
- [ ] Work objects are only ever written through `UpsertWorkObject` / `TransitionWorkObject`.
- [ ] `(account_ref, wo_type, idem_key)` uniqueness is enforced; re-processing an event does not create a duplicate.
- [ ] Shuffled event orderings for one aggregate fold to an identical final state (property test).
- [ ] An illegal transition is rejected and audited.
- [ ] Every work object retains provenance spans to its source messages.

## Test seams

- Pure `fold` unit tested with shuffled event lists (commutativity property test).
- Transition legality tested per type as a pure function.
- Integration test: events → work objects on ephemeral Postgres via the mutation layer.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[H · SWE]` Ordering named not specified | fold by `occurred_at` per aggregate; commutative transitions (not a timed buffer) |
| `[H · SWE]` Mutation layer catch-all | work objects only via `UpsertWorkObject`/`TransitionWorkObject`; legal-transition invariant |
| `[H · SWE]` Idempotency | `(account_ref, wo_type, idem_key)` uniqueness |
| `[M, folded]` Two-stage semantic dedup deferred | v1 event idempotency only; semantic dedup is fast-follow |

## Open items

- Full per-type field schemas and complete transition tables.
- Which types are surfaced in which digest section (coordinate with spec 06).
