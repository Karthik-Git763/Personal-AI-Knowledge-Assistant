from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class BackupObjectMetadata:
    drive_owner_id: UUID
    workspace_owner_id: UUID
    backup_id: UUID
    schema_version: int
    archive_checksum: str
    created_at: datetime


@dataclass(frozen=True)
class StoredBackup:
    remote_id: str
    name: str
    size: int
    created_at: datetime
    metadata: BackupObjectMetadata
    completed: bool


class BackupStore(Protocol):
    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup: ...

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]: ...

    async def download(self, remote_id: str, destination: Path) -> Path: ...

    async def delete(self, remote_id: str) -> None: ...


class BackupCleanupError(RuntimeError):
    """A retention cleanup failed after a backup had already uploaded successfully."""

    def __init__(self, remote_id: str) -> None:
        super().__init__(f"Could not delete expired backup {remote_id}")
        self.remote_id = remote_id


_CHECKSUM = re.compile(r"[0-9a-f]{64}")


def is_valid_stored_backup(backup: StoredBackup, drive_owner_id: UUID) -> bool:
    return (
        is_trusted_stored_backup(backup, drive_owner_id)
        and backup.completed
    )


def is_trusted_stored_backup(backup: StoredBackup, drive_owner_id: UUID) -> bool:
    """Return whether a listed Cognolith object is safe to authorize for deletion."""
    metadata = backup.metadata
    return (
        metadata.drive_owner_id == drive_owner_id
        and bool(backup.remote_id)
        and bool(backup.name)
        and backup.size >= 0
        and metadata.schema_version > 0
        and bool(_CHECKSUM.fullmatch(metadata.archive_checksum))
        and _is_aware(backup.created_at)
        and _is_aware(metadata.created_at)
    )


async def prune_successful_backups(
    store: BackupStore, drive_owner_id: UUID, keep: int = 5
) -> list[str]:
    if keep < 0:
        raise ValueError("keep must be zero or greater")

    backups = await store.list(drive_owner_id)
    eligible = sorted(
        (backup for backup in backups if is_valid_stored_backup(backup, drive_owner_id)),
        key=lambda backup: backup.metadata.created_at,
        reverse=True,
    )
    deleted: list[str] = []
    for backup in eligible[keep:]:
        try:
            await store.delete(backup.remote_id)
        except Exception as error:
            raise BackupCleanupError(backup.remote_id) from error
        deleted.append(backup.remote_id)
    return deleted


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
