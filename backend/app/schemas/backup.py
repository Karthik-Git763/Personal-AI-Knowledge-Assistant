from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.backup import BackupStatus, BackupTrigger, DriveConnectionStatus


class GoogleDriveConnectionStatusResponse(BaseModel):
    status: DriveConnectionStatus
    google_email: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None


class GoogleDriveAuthorizationUrlResponse(BaseModel):
    authorization_url: str


class WorkspaceBackupResponse(BaseModel):
    id: int
    status: BackupStatus
    trigger: BackupTrigger
    schema_version: int
    archive_size_bytes: int | None = None
    item_counts: dict[str, int] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_message: str | None = None


class RestorePreviewResponse(BaseModel):
    backup_id: int
    schema_version: int
    item_counts: dict[str, int] = Field(default_factory=dict)
    archive_size_bytes: int | None = None


class RestoreConfirmationRequest(BaseModel):
    backup_id: int = Field(gt=0)
    confirm: Literal[True]


class BackupOperationStatusResponse(BaseModel):
    status: Literal["accepted", "completed", "failed"]
    backup_id: int | None = None
