from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import DateTime, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Column, Field, Index, Relationship, SQLModel

from app.utils.sanitization import sanitize_plain_text

from .chat import TimestampMixin

if TYPE_CHECKING:
    from .user import User


def sanitize_backup_failure_message(value: str | None) -> str | None:
    return sanitize_plain_text(value) if value is not None else None


class BackupStatus(StrEnum):
    pending = "pending"
    exporting = "exporting"
    uploading = "uploading"
    completed = "completed"
    failed = "failed"


class BackupTrigger(StrEnum):
    manual = "manual"
    scheduled = "scheduled"


class BackupOperationKind(StrEnum):
    snapshot = "snapshot"
    restore = "restore"


class DriveConnectionStatus(StrEnum):
    connected = "connected"
    disconnected = "disconnected"
    reauthorization_required = "reauthorization_required"
    failed = "failed"


class GoogleDriveConnection(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "google_drive_connections"  # pyright: ignore
    __table_args__ = (
        Index("ix_google_drive_connections_user_status", "user_id", "status"),
        Index(
            "ix_google_drive_connections_google_subject",
            "google_subject",
            unique=True,
            postgresql_where=text(
                "status IN ('connected', 'reauthorization_required')"
            ),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, unique=True, index=True)
    encrypted_refresh_token: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    token_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    google_subject: str = Field(nullable=False, max_length=255)
    google_email: str = Field(nullable=False, max_length=255)
    granted_scopes: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    status: DriveConnectionStatus = Field(
        default=DriveConnectionStatus.connected, sa_column=Column(String(32), nullable=False)
    )
    connected_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    disconnected_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    user: "User" = Relationship(back_populates="google_drive_connection")


class OAuthState(SQLModel, table=True):
    __tablename__: ClassVar[str] = "oauth_states"  # pyright: ignore
    __table_args__ = (
        Index("ix_oauth_states_user_expires", "user_id", "expires_at"),
        Index("ix_oauth_states_expires_at", "expires_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    state_hash: str = Field(nullable=False, max_length=64, unique=True, index=True)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    user: "User" = Relationship(back_populates="oauth_states")


class WorkspaceBackup(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "workspace_backups"  # pyright: ignore
    __table_args__ = (
        Index("ix_workspace_backups_user_created", "user_id", "created_at"),
        Index("ix_workspace_backups_user_status", "user_id", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    backup_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int = Field(foreign_key="users.id", nullable=False, index=True)
    operation_kind: BackupOperationKind = Field(
        default=BackupOperationKind.snapshot,
        sa_column=Column(String(20), nullable=False),
    )
    source_backup_id: UUID | None = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), nullable=True, index=True),
    )
    remote_file_id: str | None = Field(default=None, max_length=255, index=True)
    status: BackupStatus = Field(default=BackupStatus.pending, sa_column=Column(String(20), nullable=False))
    trigger: BackupTrigger = Field(default=BackupTrigger.manual, sa_column=Column(String(20), nullable=False))
    schema_version: int = Field(default=1, nullable=False)
    archive_size_bytes: int | None = Field(default=None)
    checksum: str | None = Field(default=None, max_length=64)
    item_counts: dict[str, int] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    completed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    failure_message: str | None = Field(default=None, max_length=1000)

    user: "User" = Relationship(back_populates="workspace_backups")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "failure_message":
            value = sanitize_backup_failure_message(value)
        super().__setattr__(name, value)

class BackupSchedule(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "backup_schedules"  # pyright: ignore

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False, unique=True, index=True)
    enabled: bool = Field(default=True)
    interval_hours: int = Field(default=24)
    last_attempt_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    last_success_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    next_due_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    consecutive_failures: int = Field(default=0)

    user: "User" = Relationship(back_populates="backup_schedule")
