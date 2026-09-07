# Ops & Platform

_Epic: E9 · Plan: docs/plans/2026-09-07-v2-shadow-mode-email-spine.md_

## Purpose

Make the system runnable, observable, recoverable, and reproducible on a single self-hosted box, with the process supervision, backups, configuration, and supply-chain hygiene the rest of the design assumes. This is the substrate the three long-lived processes (`poller`, `ingest-worker`, `digest-scheduler`) run on.

## Scope

**In v1**
- `docker-compose` for Postgres + Ollama + n8n; local dev parity.
- systemd units (`restart=always`) for `poller`, `ingest-worker`, `digest-scheduler`.
- Telegram heartbeat alert when no successful poll occurs within N minutes.
- `pg_dump` backups + a documented, drilled restore procedure.
- Structured logging with rotation.
- Config via pydantic-settings + `.env.example` (no real secrets — public repo).
- `make dev` / `make test`; Alembic forward-only migrations.
- Supply-chain pinning/hashing + SBOM; minimal n8n node set.
- Data-correction protocol (compensating supersede writes; cold-partition compaction); raw-payload TTL separate from derived facts.
- CI structure: inner-loop unit/integration on ephemeral Postgres; separate model-hitting suites.

**Deferred / out of scope**
- Multi-node / HA deployment.
- Push ingestion infra (no inbound endpoint in v1).
- Attachment/document storage infra (fast-follow 1).

## Interfaces & data contracts

**Process inventory (systemd units, each `Restart=always`):**
```
ai-assistant-poller.service          # polls history.list, enqueues (no DB writes to state)
ai-assistant-ingest-worker.service   # the ONLY state writer (single consumer)
ai-assistant-digest-scheduler.service
```

**docker-compose services:** `postgres` (authoritative store + queue), `ollama` (local cheap tier), `n8n` (optional transport, holds no tokens/mail). Each runs as a distinct least-privileged user/container (isolation per spec 07).

**Config (pydantic-settings; secrets resolved from the store, not the env file in prod):**
```
# .env.example ships with placeholders ONLY — never real values (public repo)
DATABASE_URL=postgresql://app:CHANGE_ME@localhost:5432/assistant
OLLAMA_BASE_URL=http://localhost:11434
POLL_INTERVAL_SECONDS=...
HEARTBEAT_MAX_SILENCE_MINUTES=...
LOG_LEVEL=INFO
GLOBAL_SPEND_CAP=...            # see spec 07
# TELEGRAM_BOT_TOKEN, credentials → secret store only
```

**Migrations:** Alembic, **forward-only** (no down-migrations relied on in prod); each migration reviewed; schema reserves valid-time columns nullable (spec 02).

**Backup/restore:**
```
make backup   # pg_dump → timestamped, gitignored artifact (never committed)
make restore  # documented drill: restore into a scratch DB and verify row counts + audit-chain integrity
```

**Data-correction protocol:** corrections are compensating **supersede** writes carrying `correction_reason` + `batch_id`; physical deletes are never used; superseded rows are compacted to a cold partition. Raw payloads carry a TTL independent of the derived facts' retention.

**Logging:** structured JSON logs with rotation; every model call logs tokens + cost + prompt-template hash + model tag + sampling params (to `model_runs`, spec 03).

## Invariants

1. `ingest-worker` is the single state writer; `poller`/backfill/n8n only enqueue.
2. Secrets never appear in the repo, the `.env.example`, or logs.
3. Migrations are forward-only and never destructive to append-only history.
4. Backups are never committed to git; a restore drill is documented and periodically executed.
5. Data corrections are compensating supersede writes; no physical deletion of history (except GDPR erasure, spec 07).
6. Every long-lived process auto-restarts and emits a heartbeat.
7. Model-hitting suites are excluded from the inner-loop CI.

## Failure modes

- **No successful poll within N minutes** → Telegram heartbeat alert fires.
- **Process crash** → systemd restarts; audit records the restart.
- **Migration failure** → abort deploy; the DB is left on the prior known-good revision.
- **Backup artifact missing at restore drill** → alert; treat as a failed drill.
- **Disk pressure from raw payloads** → TTL/compaction reclaims space without touching derived facts.

## Acceptance criteria

- [ ] `docker-compose up` brings up Postgres + Ollama + n8n for local dev.
- [ ] systemd units for the three processes exist with `Restart=always` and are documented.
- [ ] Heartbeat alert fires to Telegram when no successful poll occurs within the configured window.
- [ ] `make backup` produces a gitignored `pg_dump`; `make restore` drill is documented and verifies audit-chain integrity.
- [ ] Structured logging with rotation is configured; secrets are never logged.
- [ ] `.env.example` is tracked with placeholders only; `.env` is gitignored; CI secret-scan passes.
- [ ] `make dev` and `make test` work from a clean checkout.
- [ ] Alembic migrations are forward-only and reserve valid-time columns nullable.
- [ ] Dependencies and model artifacts are pinned + hashed (lockfiles); an SBOM is produced; the n8n node set is minimal.
- [ ] Data corrections are compensating supersede writes with `correction_reason` + `batch_id`; superseded rows compact to a cold partition.
- [ ] Raw-payload TTL is independent of derived-fact retention.
- [ ] Inner-loop CI runs unit + integration on an ephemeral Postgres (testcontainers/template DB); model-hitting suites run separately.

## Test seams

- Integration tests run against a real ephemeral Postgres (testcontainers or template DB), not a mock.
- `Clock` injected for heartbeat/poll-interval logic.
- Backup/restore drill scripted and asserted on row counts + audit-chain verification.
- Config loading unit-tested (missing-secret → fail closed, per spec 07).

## Reviewer-finding traceability

| Finding (plan) | How this spec addresses it |
|---|---|
| `[C · SWE] No runtime owns the one deterministic writer` | Explicit process inventory; single `ingest-worker` writer; producers enqueue only. |
| `[M, folded] Ops` | docker-compose; systemd `restart=always`; Telegram heartbeat; `pg_dump` + restore drill; structured logging + rotation; pydantic-settings + `.env.example`; `make dev/test`. |
| `[M, folded] Data-correction protocol` | Compensating supersede writes with `correction_reason` + batch id; cold-partition compaction; never physical delete. |
| `[M, folded] Supply chain` | Pin + hash deps and model artifacts; SBOM; minimal n8n nodes. |
| `[M, folded] Privacy` (raw payload TTL) | Raw payloads TTL'd separately from derived facts. |
| `[L, folded] Suite separation / reproducibility` | Model-hitting suites separated from inner-loop CI; model_runs logs prompt hash/model/params. |
| Integration on real ephemeral Postgres | testcontainers/template DB in CI. |

## Open items

- Target box RAM/GPU sizing (drives Ollama model choice — see spec 03/04 and plan open items).
- Backup cadence + retention window; offsite copy policy.
- SBOM tooling choice; secret-scan tool in CI.
