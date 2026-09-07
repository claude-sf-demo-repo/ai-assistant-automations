"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, Sequence[str], None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


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
