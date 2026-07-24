from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.services.backup_store import BackupObjectMetadata, StoredBackup


@dataclass
class FakeBackupStore:
    drive_owner_id: UUID
    backups: list[StoredBackup] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    fail_deletes: set[str] = field(default_factory=set)
    incomplete: StoredBackup | None = None
    foreign: StoredBackup | None = None
    oldest_successful: StoredBackup | None = None

    @classmethod
    def with_backups(cls, successful: int, incomplete: int, foreign: int = 0) -> FakeBackupStore:
        drive_owner_id = uuid4()
        now = datetime.now(UTC)
        backups = [
            _backup(drive_owner_id, index, now - timedelta(minutes=index), completed=True)
            for index in range(successful)
        ]
        store = cls(drive_owner_id=drive_owner_id, backups=backups)
        if backups:
            store.oldest_successful = backups[-1]
        if incomplete:
            store.incomplete = _backup(
                drive_owner_id, successful + 1, now - timedelta(minutes=successful + 1), completed=False
            )
            store.backups.append(store.incomplete)
        if foreign:
            store.foreign = _backup(
                uuid4(), successful + incomplete + 1, now - timedelta(days=1), completed=True
            )
            store.backups.append(store.foreign)
        return store

    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup:
        backup = StoredBackup(
            remote_id=f"fake_backup_{metadata.backup_id.hex}",
            name=archive.name,
            size=archive.stat().st_size,
            created_at=metadata.created_at,
            metadata=metadata,
            completed=True,
        )
        self.backups.append(backup)
        return backup

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]:
        return list(self.backups)

    async def download(self, remote_id: str, destination: Path) -> Path:
        destination.write_bytes(remote_id.encode())
        return destination

    async def delete(self, remote_id: str) -> None:
        if remote_id in self.fail_deletes:
            raise RuntimeError("configured deletion failure")
        self.deleted.append(remote_id)


def _backup(drive_owner_id: UUID, index: int, created_at: datetime, *, completed: bool) -> StoredBackup:
    metadata = BackupObjectMetadata(
        drive_owner_id=drive_owner_id,
        workspace_owner_id=uuid4(),
        backup_id=uuid4(),
        schema_version=1,
        archive_checksum=f"{index:064x}",
        created_at=created_at,
    )
    return StoredBackup(
        remote_id=f"fake_backup_{index:08d}",
        name=f"backup-{index}.zip",
        size=1024,
        created_at=created_at,
        metadata=metadata,
        completed=completed,
    )
