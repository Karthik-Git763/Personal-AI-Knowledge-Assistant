from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.backup_store import (
    BackupCleanupError,
    BackupObjectMetadata,
    StoredBackup,
    prune_successful_backups,
)
from tests.fakes.fake_backup_store import FakeBackupStore


@pytest.mark.asyncio
async def test_retention_deletes_only_successful_backups_after_newest_five() -> None:
    store = FakeBackupStore.with_backups(successful=6, incomplete=1)
    assert store.oldest_successful is not None
    assert store.incomplete is not None

    deleted = await prune_successful_backups(store, drive_owner_id=store.drive_owner_id, keep=5)

    assert deleted == [store.oldest_successful.remote_id]
    assert store.incomplete.remote_id not in deleted


@pytest.mark.asyncio
async def test_retention_preserves_foreign_and_malformed_backups() -> None:
    store = FakeBackupStore.with_backups(successful=6, incomplete=0, foreign=1)
    assert store.oldest_successful is not None
    assert store.foreign is not None
    malformed = StoredBackup(
        remote_id="malformed_123",
        name="malformed.zip",
        size=1,
        created_at=datetime.now(UTC),
        metadata=BackupObjectMetadata(
            drive_owner_id=store.drive_owner_id,
            workspace_owner_id=uuid4(),
            backup_id=uuid4(),
            schema_version=1,
            archive_checksum="not-a-checksum",
            created_at=datetime.now(UTC),
        ),
        completed=True,
    )
    store.backups.append(malformed)

    deleted = await prune_successful_backups(store, drive_owner_id=store.drive_owner_id)

    assert deleted == [store.oldest_successful.remote_id]
    assert store.foreign.remote_id not in deleted
    assert malformed.remote_id not in deleted


@pytest.mark.asyncio
async def test_retention_raises_cleanup_error_without_mutating_backup_completion() -> None:
    store = FakeBackupStore.with_backups(successful=6, incomplete=1)
    assert store.oldest_successful is not None
    store.fail_deletes.add(store.oldest_successful.remote_id)
    before = [(backup.remote_id, backup.completed) for backup in store.backups]

    with pytest.raises(BackupCleanupError, match=store.oldest_successful.remote_id):
        await prune_successful_backups(store, drive_owner_id=store.drive_owner_id)

    assert [(backup.remote_id, backup.completed) for backup in store.backups] == before


@pytest.mark.asyncio
async def test_retention_rejects_negative_keep_count() -> None:
    store = FakeBackupStore.with_backups(successful=1, incomplete=0)

    with pytest.raises(ValueError, match="keep"):
        await prune_successful_backups(store, drive_owner_id=store.drive_owner_id, keep=-1)
