from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.backup import (
    BackupOperationKind,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
)


class GoogleDriveConnectionStatusResponse(BaseModel):
    status: DriveConnectionStatus
    google_email: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None


class GoogleDriveAuthorizationUrlResponse(BaseModel):
    authorization_url: str


class WorkspaceBackupResponse(BaseModel):
    backup_id: UUID
    operation_kind: BackupOperationKind
    source_backup_id: UUID | None = None
    status: BackupStatus
    trigger: BackupTrigger
    schema_version: int
    archive_size_bytes: int | None = None
    item_counts: dict[str, int] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_message: str | None = None


class RestorePreviewResponse(BaseModel):
    backup_id: UUID
    schema_version: int
    item_counts: dict[str, int] = Field(default_factory=dict)
    archive_size_bytes: int | None = None


class BackupPreview(BaseModel):
    created_at: datetime
    schema_version: int
    app_version: str
    archive_size_bytes: int
    item_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RestoreResult(BaseModel):
    reprocessing_document_ids: list[int] = Field(default_factory=list)


class RestoreConfirmationRequest(BaseModel):
    backup_id: UUID
    confirmation: Literal["RESTORE"]


class BackupOperationStatusResponse(BaseModel):
    status: BackupStatus
    backup_id: UUID | None = None
