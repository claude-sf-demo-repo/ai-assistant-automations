"""DLQ replay (issue #16): re-enqueue a dead-lettered row back onto `ingest_queue`.

Per spec 01 ("DLQ") replay is "copy a DLQ row back to `ingest_queue` (status `ready`)".
Idempotent by construction: `QueueRepository.enqueue_from_dlq` reuses the exact
`ON CONFLICT (idempotency_key) DO NOTHING` pattern Task 4's `PostgresIngestTransaction.
enqueue_many` established, so replaying the same DLQ row twice (or replaying a row whose
`idempotency_key` is somehow already present in `ingest_queue` in any status) creates at
most one queue row -- never a duplicate, never a raised error.

Both `queue_repo` and `dlq_repo` are expected to be `QueueRepository` instances bound to
the same connection/transaction as the caller (matching every other repository call
site in this codebase -- the caller owns commit/rollback), so the DLQ-row deletion and
the queue re-insertion become durable atomically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.repositories.queue import QueueRepository

__all__ = ["replay"]


def replay(dlq_repo: "QueueRepository", queue_repo: "QueueRepository", dlq_id: int) -> None:
    """Replay one `ingest_dlq` row (by id) back onto `ingest_queue`.

    If the DLQ row no longer exists (e.g. already replayed and deleted by a concurrent
    or prior call), this is a silent no-op -- consistent with the idempotency guarantee
    the brief asks for ("processed exactly once ... even if replayed twice"). `dlq_repo`
    and `queue_repo` are typically the same `QueueRepository` instance (both DLQ and
    queue access live on that one class per this module's docstring); they're accepted
    as separate parameters only to match the brief's literal signature.
    """
    dlq_row = dlq_repo.get_dlq_row(dlq_id)
    if dlq_row is None:
        return
    queue_repo.enqueue_from_dlq(dlq_row)
    dlq_repo.delete_dlq_row(dlq_id)
