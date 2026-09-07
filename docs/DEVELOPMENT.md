# Development

## Setup

```bash
python3.11 -m venv .venv        # or any Python >=3.11
source .venv/bin/activate
cp .env.example .env             # then edit DATABASE_URL / GMAIL_OAUTH_TOKEN_PATH for your machine
make dev                         # pip install -e .[dev,gmail]
```

## Common commands

| Command | Does |
|---|---|
| `make dev` | Installs the package and dev/gmail extras in editable mode (`pip install -e .[dev,gmail]`). |
| `make up` | Starts the dev Postgres container (`docker compose up -d`). Pinned to `postgres:16`. |
| `make down` | Stops the dev Postgres container. |
| `make test` | Runs `pytest tests/unit tests/integration`. |
| `alembic upgrade head` | Applies migrations against `DATABASE_URL` (read from `.env` / environment via `shared/config/settings.py`; never hardcoded). |

## Gmail OAuth token

The Gmail OAuth token file is **never committed**. It lives outside the repo (or anywhere
covered by `.gitignore`, e.g. a top-level `*.token.json`) at the path pointed to by the
`GMAIL_OAUTH_TOKEN_PATH` environment variable / `.env` entry. Treat this file as the secrets
store for v1 — a real secrets manager (cloud KMS-backed store, etc.) is a documented
fast-follow, not required for E1.

## Injectable seams

All I/O (wall-clock reads, model calls, external tool calls, persistence) goes through a
protocol defined in `shared/events/seams.py` (`Clock`, `ModelClient`, `ToolClient`, `Store`).
This task provides the protocols plus:

- `shared/events/clock.py::SystemClock` — the real wall-clock implementation.
- `tests/unit/fakes.py::FakeClock` — a deterministic, settable/advanceable fake for tests.

Fakes for `ModelClient`, `ToolClient`, and `Store` land alongside their real implementations
in later tasks/epics.

## Process inventory

Per spec 01 ("Processes"), exactly three long-running processes exist in v1, plus an
optional transport:

| Process | Count | Role | Entry point |
|---|---|---|---|
| `poller` | 1 | Polls Gmail `history.list`, validates envelopes, enqueues them. Holds the OAuth token. **Never writes state.** | `adapters/gmail/adapter.py::poll_once` |
| `ingest-worker` | 1 | The **only** consumer/DB writer: claims from `ingest_queue` (`FOR UPDATE SKIP LOCKED`), holds a per-`thread_ref` advisory lock (`pg_advisory_xact_lock`) for the duration of each item, folds event-time state, acks/retries/DLQs. | `core/ingestion/worker.py::run_forever` |
| `digest-scheduler` | 1 | Reads state, emits the daily Telegram digest. **Not implemented in E1** — stub only, lands with spec 06. | — |
| n8n | optional | Transport only: moves opaque notifications, holds no tokens and no mail. | — |

Enqueue-only producers (the poller, and n8n if wired) never call anything in
`core/ingestion/worker.py` or open a write transaction against `ingest_queue`/
`ingest_dlq`/`thread_fold_projection` directly — the single `ingest-worker` process is
the only writer, matching spec 01 invariant 1 ("Exactly one process writes to state").

## Forward-only migrations

Every Alembic migration's `downgrade()` raises `NotImplementedError()` (see
`storage/migrations/script.py.mako`). Per spec 09's ops policy, migrations are forward-only
and never destructive to append-only history — rolling back a bad migration means writing a
new forward migration, not reversing one.
