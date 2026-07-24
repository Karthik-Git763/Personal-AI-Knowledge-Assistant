"""add Google Drive backup foundation

Revision ID: 0005_google_drive_backup_foundation
Revises: 0004_streaming_chat
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_google_drive_backup_foundation"
down_revision: str | Sequence[str] | None = "0004_streaming_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PORTABLE_ID_TABLES = (
    "users",
    "note_templates",
    "notes",
    "note_folders",
    "note_tags",
    "note_links",
    "documents",
    "chat_sessions",
    "chat_messages",
)


def upgrade() -> None:
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    for table_name in PORTABLE_ID_TABLES:
        op.add_column(table_name, sa.Column("portable_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.execute(f"UPDATE {table_name} SET portable_id = gen_random_uuid() WHERE portable_id IS NULL")
        op.alter_column(table_name, "portable_id", nullable=False)
        op.create_index(f"ix_{table_name}_portable_id", table_name, ["portable_id"], unique=True)

    op.create_table(
        "google_drive_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_subject", sa.String(length=255), nullable=False),
        sa.Column("google_email", sa.String(length=255), nullable=False),
        sa.Column("granted_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index(
        "ix_google_drive_connections_user_status",
        "google_drive_connections",
        ["user_id", "status"],
        unique=False,
    )

    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_hash"),
    )
    op.create_index("ix_oauth_states_user_expires", "oauth_states", ["user_id", "expires_at"])
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])

    op.create_table(
        "workspace_backups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("backup_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("remote_file_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("archive_size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("item_counts", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workspace_backups_user_created", "workspace_backups", ["user_id", "created_at"])
    op.create_index("ix_workspace_backups_user_status", "workspace_backups", ["user_id", "status"])
    op.create_index("ix_workspace_backups_backup_id", "workspace_backups", ["backup_id"], unique=True)

    op.create_table(
        "backup_schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_hours", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("backup_schedules")
    op.drop_index("ix_workspace_backups_backup_id", table_name="workspace_backups")
    op.drop_index("ix_workspace_backups_user_status", table_name="workspace_backups")
    op.drop_index("ix_workspace_backups_user_created", table_name="workspace_backups")
    op.drop_table("workspace_backups")
    op.drop_index("ix_oauth_states_expires_at", table_name="oauth_states")
    op.drop_index("ix_oauth_states_user_expires", table_name="oauth_states")
    op.drop_table("oauth_states")
    op.drop_index("ix_google_drive_connections_user_status", table_name="google_drive_connections")
    op.drop_table("google_drive_connections")

    for table_name in reversed(PORTABLE_ID_TABLES):
        op.drop_index(f"ix_{table_name}_portable_id", table_name=table_name)
        op.drop_column(table_name, "portable_id")
