# Ingestion & Event Backbone

_Epic: E1 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Turn Gmail into a stream of validated Common Event Envelopes and deliver them to exactly one
deterministic writer. Owns the poller, envelope validation, idempotency, the Postgres work
queue, the single-consumer `ingest-worker`, the dead-letter queue, replay, and event-time
ordering. This is the backbone every downstream subsystem consumes.

## Scope

**In v1**
- Poll Gmail `history.list`; persist the `historyId` cursor transactionally with the events it produced.
- Validate envelopes (spec 00); enforce idempotency.
- Postgres work queue (`LISTEN/NOTIFY` + `FOR UPDATE SKIP LOCKED`).
- Single-consumer `ingest-worker` — the **only** DB writer — with per-`thread_id` advisory lock.
- DLQ table + retry/backoff/poison threshold; replay by re-enqueue.
- Event-time ordering (fold by `occurred_at`).

**Deferred / out of scope**
- Push (Pub/Sub) ingestion — poll only in v1 (no inbound endpoint).
- Non-Gmail adapters (same queue, additive later).

## Interfaces & data contracts

### Processes (process inventory)

| Process | Count | Role |
|---|---|---|
| `poller` | 1 | polls `history.list`, enqueues envelopes; holds OAuth; **never writes state** |
| `ingest-worker` | 1 | the **only** consumer/DB writer; runs the pipeline (spec 02–05) |
| `digest-scheduler` | 1 | reads state, emits digest (spec 06) |
| n8n | optional | transport only; holds **no** tokens and **no** mail |

The poller holds the OAuth token and fetches messages (scope `gmail.readonly`). n8n, if used,
only moves opaque notifications — it never sees credentials or message bodies.

### Poller loop

```python
def poll_once(gmail: ToolClient, store: Store, clock: Clock) -> None:
    cursor = store.get_cursor("gmail", account_ref)          # last historyId
    changes = gmail.call("history.list", {"startHistoryId": cursor})
    envelopes = [to_envelope(c) for c in changes.records]     # adapters/gmail
    # cursor advance + enqueue committed in ONE transaction:
    with store.transaction() as tx:
        tx.enqueue_many(envelopes)                            # idempotent (unique key)
        tx.set_cursor("gmail", account_ref, changes.new_history_id)
```

If the process dies between enqueue and cursor-advance, the transaction rolls back and the poll
is retried from the old cursor — re-delivered events are absorbed by the idempotency key.

### Work queue (Postgres)

```sql
CREATE TABLE ingest_queue (
  id              bigserial PRIMARY KEY,
  idempotency_key text NOT NULL UNIQUE,     -- source_system_id + '|' + type
  account_ref     text NOT NULL,
  thread_ref      text NOT NULL,
  envelope        jsonb NOT NULL,
  status          text NOT NULL DEFAULT 'ready',   -- ready|leased|done|dead
  attempts        int  NOT NULL DEFAULT 0,
  not_before      timestamptz NOT NULL DEFAULT now(),  -- backoff
  enqueued_at     timestamptz NOT NULL DEFAULT now()
);
```

Consumer claim (single worker, but pattern is concurrency-safe):

```sql
SELECT * FROM ingest_queue
 WHERE status='ready' AND not_before <= now()
 ORDER BY id
 FOR UPDATE SKIP LOCKED
 LIMIT 1;
```

The worker takes a **per-`thread_ref` advisory lock** (`pg_advisory_xact_lock(hashtext(thread_ref))`)
so all mutations for a thread serialize even if the queue is later parallelized.

### DLQ

```sql
CREATE TABLE ingest_dlq (
  id bigserial PRIMARY KEY, idempotency_key text, envelope jsonb,
  error text, attempts int, died_at timestamptz DEFAULT now()
);
```

Retry with exponential backoff (set `not_before`); after `POISON_THRESHOLD` attempts move to
`ingest_dlq` and continue. Replay = copy a DLQ row back to `ingest_queue` (status `ready`).

## Invariants

1. Exactly one process writes to state (`ingest-worker`); poller and n8n only enqueue.
2. Cursor advance and the events it produced commit atomically (same transaction).
3. Re-delivery of an event with an existing `idempotency_key` is a no-op (unique constraint).
4. All work for a given `thread_ref` is serialized (advisory lock).
5. Ordering/folding uses `occurred_at`, never queue arrival order.
6. A poison message lands in the DLQ and never blocks the queue head.

## Failure modes

- **Duplicate delivery** → unique-constraint conflict → treated as already-enqueued (no error surfaced).
- **Poller crash mid-poll** → transaction rollback → safe re-poll from old cursor.
- **Handler exception** → increment `attempts`, set backoff; at threshold → DLQ + alert.
- **Out-of-order delivery** (`replied` before `received`) → folded correctly by `occurred_at` (spec 05).
- **Queue backlog** → per-source-fair bounds + backpressure (spec 07 denial-of-wallet).

## Acceptance criteria

- [ ] `poller` advances `historyId` and enqueues envelopes in a single transaction; a simulated crash between the two leaves no gap and no double-write.
- [ ] Enqueuing the same event twice results in exactly one queue row (unique `idempotency_key`).
- [ ] `ingest-worker` claims work via `FOR UPDATE SKIP LOCKED` and holds a per-`thread_ref` advisory lock for the duration of processing.
- [ ] Two concurrent enqueues for the same thread are serialized; no double-invalidation of state occurs.
- [ ] A handler that raises is retried with backoff and moved to `ingest_dlq` after `POISON_THRESHOLD`; the queue continues.
- [ ] A DLQ row can be replayed back onto the queue and processed idempotently.
- [ ] Shuffled `received`/`replied` events for one thread converge to the same folded state as in-order delivery.
- [ ] n8n (if wired) receives only opaque notifications — no token, no body — verified by inspecting what it is given.

## Test seams

- `ToolClient` fakes Gmail `history.list`; `Store` is a real ephemeral Postgres (testcontainers) for queue/lock tests, in-memory fake for pure logic.
- Integration test: end-to-end enqueue → claim → process on real Postgres.
- Property test: N shuffled orderings of a thread's events → identical folded state.

## Reviewer-finding traceability

| Finding | Addressed by |
|---|---|
| `[C · SWE]` No runtime owns the single deterministic writer | process inventory; enqueue-only producers; one `ingest-worker`; per-thread advisory lock |
| `[H · SWE]` Idempotency/DLQ/replay/ordering named not specified | unique `idempotency_key`; DLQ table + backoff + poison threshold; replay; fold by `occurred_at` |
| `[H · Security]` Endpoint blast radius | poll only (no inbound endpoint); n8n holds no tokens/mail |
| `[C · Security]` Trusted-signal boundary | `thread_ref` = server thread id carried through (spec 00/07) |

## Open items

- `history.list` page-size / rate-limit tuning and the poll interval.
- Whether to keep n8n at all in v1 or have the poller enqueue directly (leaning: poller direct; n8n optional).
