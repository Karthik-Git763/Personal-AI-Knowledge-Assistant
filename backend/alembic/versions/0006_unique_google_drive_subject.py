"""enforce one local user per Google Drive identity

Revision ID: 0006_unique_google_drive_subject
Revises: 0005_google_drive_backup_foundation
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_unique_google_drive_subject"
down_revision: str | Sequence[str] | None = "0005_google_drive_backup_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_google_drive_connections_google_subject"


def upgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT 1
            FROM google_drive_connections
            GROUP BY google_subject
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot enforce Google Drive account ownership because duplicate "
            "Google subjects exist. Disconnect or remove duplicate local Drive "
            "connections, then rerun the migration."
        )
    op.create_index(
        _INDEX_NAME,
        "google_drive_connections",
        ["google_subject"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="google_drive_connections")
