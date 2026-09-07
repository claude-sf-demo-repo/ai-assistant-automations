"""thread_fold_projection: E1-only stand-in for the E2 mutation/state layer (issue #17).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-07

See `storage/repositories/thread_fold.py`'s module docstring for the full rationale.
Short version: this table is NOT the fact/edge store (spec 02). It exists so E1 can
prove event-time ordering (`core/ingestion/fold.py::fold_thread_state`) end-to-end,
through the real queue and a real per-thread advisory lock, without first building
Epic E2's mutation layer. Epic E2 will fold events through `core/mutation.apply()`
instead; this projection is expected to be superseded/removed then.

Columns:
- `account_ref`, `thread_ref`: composite primary key (every row is scoped to one
  thread within one account, matching the envelope's own scoping -- spec 00 invariant
  3).
- `state jsonb`: the repository's own serialization of the accumulated source events
  plus the derived `ThreadFoldState` (see `storage/repositories/thread_fold.py` for the
  exact shape) -- deliberately opaque to this migration, which only owns the column
  type.
- `updated_at`: last write time, from the app's `Clock` seam, not `now()` -- matches
  the "no wall-clock reads outside `Clock`" invariant (spec 00 invariant 5) rather than
  delegating to Postgres's own clock. Repository still passes a value in explicitly.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE thread_fold_projection (
          account_ref text NOT NULL,
          thread_ref  text NOT NULL,
          state       jsonb NOT NULL,
          updated_at  timestamptz NOT NULL,
          PRIMARY KEY (account_ref, thread_ref)
        )
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    Forward-only migration policy (spec 09): destructive downgrades are never implemented.
    Rolling back a bad migration means writing and applying a new forward migration that
    fixes it, not reversing history. This keeps the audit trail (and any data already
    written under the new schema) intact.
    """
    raise NotImplementedError(
        "Forward-only migrations (spec 09): write a new forward migration instead of downgrading."
    )
