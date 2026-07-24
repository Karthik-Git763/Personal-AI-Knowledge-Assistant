from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.models.backup import (
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
    GoogleDriveConnection,
    WorkspaceBackup,
)
from app.models.user import User
from app.services.backup_archive import BackupExportResult, BackupManifestV1
from app.services.backup_coordinator import BackupCoordinator
from app.services.backup_store import BackupObjectMetadata, StoredBackup
from app.services.google_drive_oauth import derive_drive_owner_id
from app.services.google_drive_store import GoogleDriveReauthorizationRequiredError


class RecordingExporter:
    def __init__(
        self,
        now: datetime,
        failure: Exception | None = None,
        manifest_owner_id: UUID | None = None,
    ) -> None:
        self.now = now
        self.failure = failure
        self.manifest_owner_id = manifest_owner_id

    def export(self, user: User, destination: Path) -> BackupExportResult:
        if self.failure is not None:
            raise self.failure
        destination.write_bytes(b"backup-archive")
        manifest = BackupManifestV1(
            schema_version=1,
            backup_id=uuid4(),
            owner_id=self.manifest_owner_id or user.portable_id,
            created_at=self.now,
            app_version="test",
            counts={"notes": 2},
            checksums={"notes.json": "a" * 64},
            record_filenames={"notes": "notes.json"},
        )
        return BackupExportResult(
            path=destination,
            manifest=manifest,
            archive_checksum="b" * 64,
            archive_size=destination.stat().st_size,
        )


class RecordingStore:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        mismatch: bool = False,
        fail_retention: bool = False,
        list_failure: BaseException | None = None,
    ) -> None:
        self.failure = failure
        self.mismatch = mismatch
        self.fail_retention = fail_retention
        self.list_failure = list_failure
        self.backups: list[StoredBackup] = []
        self.deleted: list[str] = []
        self.listed_drive_owner_ids: list[UUID] = []

    @property
    def remote_ids(self) -> set[str]:
        return {backup.remote_id for backup in self.backups}

    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup:
        if self.failure is not None:
            raise self.failure
        returned_metadata = metadata
        if self.mismatch:
            returned_metadata = BackupObjectMetadata(
                drive_owner_id=uuid4(),
                workspace_owner_id=metadata.workspace_owner_id,
                backup_id=metadata.backup_id,
                schema_version=metadata.schema_version,
                archive_checksum=metadata.archive_checksum,
                created_at=metadata.created_at,
            )
        stored = StoredBackup(
            remote_id=f"remote-{metadata.backup_id.hex}",
            name=archive.name,
            size=archive.stat().st_size,
            created_at=metadata.created_at,
            metadata=returned_metadata,
            completed=True,
        )
        self.backups.append(stored)
        return stored

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]:
        self.listed_drive_owner_ids.append(drive_owner_id)
        if self.list_failure is not None:
            raise self.list_failure
        return list(self.backups)

    async def download(self, remote_id: str, destination: Path) -> Path:
        destination.write_bytes(remote_id.encode())
        return destination

    async def delete(self, remote_id: str) -> None:
        if self.fail_retention:
            raise RuntimeError("provider response: refresh-token-secret")
        self.deleted.append(remote_id)


class LifecycleStore(RecordingStore):
    def __init__(self, session: Session, user_id: int) -> None:
        super().__init__()
        self.session = session
        self.user_id = user_id
        self.status_during_upload: BackupStatus | str | None = None

    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup:
        backup = self.session.exec(
            select(WorkspaceBackup)
            .where(WorkspaceBackup.user_id == self.user_id)
            .order_by(col(WorkspaceBackup.id).desc())
        ).one()
        self.status_during_upload = backup.status
        return await super().upload(archive, metadata)


class LifecycleExporter(RecordingExporter):
    def __init__(self, now: datetime, session: Session, user_id: int) -> None:
        super().__init__(now)
        self.session = session
        self.user_id = user_id
        self.status_during_export: BackupStatus | str | None = None

    def export(self, user: User, destination: Path) -> BackupExportResult:
        backup = self.session.exec(
            select(WorkspaceBackup)
            .where(WorkspaceBackup.user_id == self.user_id)
            .order_by(col(WorkspaceBackup.id).desc())
        ).one()
        self.status_during_export = backup.status
        return super().export(user, destination)


class BlockingStore(RecordingStore):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("upload should be cancelled")


@pytest.fixture
def backup_workspace(session: Session) -> tuple[User, GoogleDriveConnection, BackupSchedule]:
    user = User(email="backup-coordinator@example.com", hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=b"encrypted",
        google_subject="subject",
        google_email=user.email,
        granted_scopes=["https://www.googleapis.com/auth/drive.appdata"],
    )
    schedule = BackupSchedule(
        user_id=user.id,
        enabled=True,
        next_due_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )
    session.add_all([connection, schedule])
    session.commit()
    return user, connection, schedule


def _coordinator(
    session: Session,
    tmp_path: Path,
    now: datetime,
    store: RecordingStore,
    *,
    exporter_failure: Exception | None = None,
    exporter: RecordingExporter | None = None,
) -> BackupCoordinator:
    configured_exporter = exporter or RecordingExporter(now, exporter_failure)
    return BackupCoordinator(
        session_factory=lambda: session,
        exporter_factory=lambda session: configured_exporter,
        store_factory=lambda session, user, connection: store,
        clock=lambda: now,
        temporary_directory=tmp_path / "backup-temp",
        close_sessions=False,
    )


@pytest.mark.asyncio
async def test_successful_backup_commits_verified_metadata_and_schedule(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, schedule = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    store = RecordingStore()
    coordinator = _coordinator(session, tmp_path, now, store)

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.completed
    assert result.remote_file_id in store.remote_ids
    assert result.archive_size_bytes == len(b"backup-archive")
    assert result.checksum == "b" * 64
    assert result.item_counts == {"notes": 2}
    assert store.backups[0].metadata.workspace_owner_id == user.portable_id
    assert store.backups[0].metadata.drive_owner_id == derive_drive_owner_id(
        connection.google_subject
    )
    assert store.listed_drive_owner_ids == [derive_drive_owner_id(connection.google_subject)]
    session.refresh(schedule)
    assert schedule.last_attempt_at == now
    assert schedule.last_success_at == now
    assert schedule.next_due_at == now + timedelta(hours=24)


@pytest.mark.asyncio
async def test_backup_persists_exporting_and_uploading_transitions_before_completion(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    store = LifecycleStore(session, user.id)  # type: ignore[arg-type]
    exporter = LifecycleExporter(now, session, user.id)  # type: ignore[arg-type]
    coordinator = _coordinator(session, tmp_path, now, store, exporter=exporter)

    pending = coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]
    result = await coordinator.run_backup(pending.backup_id)

    assert pending.status == BackupStatus.pending
    assert exporter.status_during_export == BackupStatus.exporting
    assert store.status_during_upload == BackupStatus.uploading
    assert result.status == BackupStatus.completed
    assert result.started_at == now


@pytest.mark.asyncio
async def test_backup_releases_advisory_lock_after_completion(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    coordinator = _coordinator(
        session,
        tmp_path,
        datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        RecordingStore(),
    )

    await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]
    released = session.connection().execute(
        text("SELECT pg_try_advisory_lock(:namespace, :user_id)"),
        {"namespace": coordinator._LOCK_NAMESPACE, "user_id": user.id},  # noqa: SLF001
    ).scalar_one()
    session.connection().execute(
        text("SELECT pg_advisory_unlock(:namespace, :user_id)"),
        {"namespace": coordinator._LOCK_NAMESPACE, "user_id": user.id},  # noqa: SLF001
    )
    session.rollback()

    assert released is True


@pytest.mark.asyncio
async def test_failed_upload_preserves_existing_backups(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    failing_store = RecordingStore(failure=RuntimeError("provider body: refresh-token-secret"))
    failing_store.backups.append(
        StoredBackup(
            remote_id="previous-backup",
            name="previous.zip",
            size=12,
            created_at=now - timedelta(days=1),
            metadata=BackupObjectMetadata(
                drive_owner_id=derive_drive_owner_id(connection.google_subject),
                workspace_owner_id=user.portable_id,
                backup_id=uuid4(),
                schema_version=1,
                archive_checksum="a" * 64,
                created_at=now - timedelta(days=1),
            ),
            completed=True,
        )
    )
    coordinator = _coordinator(session, tmp_path, now, failing_store)
    previous_ids = failing_store.remote_ids.copy()

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.failed
    assert failing_store.remote_ids == previous_ids
    assert result.failure_message == "Backup failed"
    assert not any((tmp_path / "backup-temp").glob("**/*.zip"))


@pytest.mark.asyncio
async def test_metadata_mismatch_fails_without_trusting_remote_file(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    coordinator = _coordinator(
        session,
        tmp_path,
        datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        RecordingStore(mismatch=True),
    )

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.failed
    assert result.remote_file_id is None
    assert result.failure_message == "Backup verification failed"


def test_duplicate_trigger_returns_pending_operation(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, _ = backup_workspace
    coordinator = _coordinator(
        session,
        tmp_path,
        datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        RecordingStore(),
    )

    first = coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]
    second = coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert second.id == first.id
    assert (
        len(
            session.exec(
                select(WorkspaceBackup).where(
                    WorkspaceBackup.user_id == user.id,
                    WorkspaceBackup.status == BackupStatus.pending,
                )
            ).all()
        )
        == 1
    )


@pytest.mark.asyncio
async def test_retention_failure_keeps_completed_backup_and_sanitizes_warning(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    store = RecordingStore(fail_retention=True)
    for index in range(5):
        store.backups.append(
            StoredBackup(
                remote_id=f"previous-{index}",
                name=f"previous-{index}.zip",
                size=12,
                created_at=now - timedelta(days=index + 1),
                metadata=BackupObjectMetadata(
                    drive_owner_id=derive_drive_owner_id(connection.google_subject),
                    workspace_owner_id=user.portable_id,
                    backup_id=uuid4(),
                    schema_version=1,
                    archive_checksum=f"{index:064x}",
                    created_at=now - timedelta(days=index + 1),
                ),
                completed=True,
            )
        )
    coordinator = _coordinator(session, tmp_path, now, store)

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.completed
    assert result.failure_message == "Backup retention cleanup failed"


@pytest.mark.asyncio
async def test_retention_list_failure_keeps_completed_backup_and_schedule_success(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, schedule = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    coordinator = _coordinator(
        session,
        tmp_path,
        now,
        RecordingStore(list_failure=RuntimeError("provider response: refresh-token-secret")),
    )

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.completed
    assert result.failure_message == "Backup retention cleanup failed"
    session.refresh(schedule)
    assert schedule.last_success_at == now
    assert schedule.next_due_at == now + timedelta(hours=24)


@pytest.mark.asyncio
async def test_retention_cancellation_keeps_completed_backup_and_schedule_success(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, schedule = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    coordinator = _coordinator(
        session,
        tmp_path,
        now,
        RecordingStore(list_failure=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    backup = session.exec(select(WorkspaceBackup).where(WorkspaceBackup.user_id == user.id)).one()
    assert backup.status == BackupStatus.completed
    assert backup.failure_message == "Backup retention cleanup failed"
    session.refresh(schedule)
    assert schedule.last_success_at == now
    assert schedule.next_due_at == now + timedelta(hours=24)


@pytest.mark.asyncio
async def test_successful_retention_keeps_the_newest_five_snapshots(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    store = RecordingStore()
    for index in range(5):
        store.backups.append(
            StoredBackup(
                remote_id=f"previous-{index}",
                name=f"previous-{index}.zip",
                size=12,
                created_at=now - timedelta(days=index + 1),
                metadata=BackupObjectMetadata(
                    drive_owner_id=derive_drive_owner_id(connection.google_subject),
                    workspace_owner_id=user.portable_id,
                    backup_id=uuid4(),
                    schema_version=1,
                    archive_checksum=f"{index:064x}",
                    created_at=now - timedelta(days=index + 1),
                ),
                completed=True,
            )
        )
    coordinator = _coordinator(session, tmp_path, now, store)

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.completed
    assert store.deleted == ["previous-4"]
    assert store.listed_drive_owner_ids == [derive_drive_owner_id(connection.google_subject)]


@pytest.mark.asyncio
async def test_revoked_connection_requires_reauthorization(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, connection, _ = backup_workspace
    coordinator = _coordinator(
        session,
        tmp_path,
        datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        RecordingStore(failure=GoogleDriveReauthorizationRequiredError("provider payload")),
    )

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.failed
    session.refresh(connection)
    assert connection.status == DriveConnectionStatus.reauthorization_required


@pytest.mark.asyncio
async def test_manifest_owner_mismatch_fails_before_upload(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    store = RecordingStore()
    coordinator = _coordinator(
        session,
        tmp_path,
        now,
        store,
        exporter=RecordingExporter(now, manifest_owner_id=uuid4()),
    )

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.failed
    assert result.failure_message == "Backup verification failed"
    assert store.backups == []


@pytest.mark.asyncio
async def test_cancellation_marks_backup_failed_then_reraises(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, _ = backup_workspace
    store = BlockingStore()
    coordinator = _coordinator(
        session,
        tmp_path,
        datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
        store,
    )
    backup = coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]
    task = asyncio.create_task(coordinator.run_backup(backup.backup_id))
    await asyncio.wait_for(store.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    persisted = session.exec(
        select(WorkspaceBackup).where(WorkspaceBackup.backup_id == backup.backup_id)
    ).one()
    assert persisted.status == BackupStatus.failed
    assert persisted.failure_message == "Backup interrupted"
    assert not any((tmp_path / "backup-temp").glob("**/*.zip"))


@pytest.mark.asyncio
async def test_export_failure_sanitizes_error_and_releases_active_operation(
    session: Session, tmp_path: Path, backup_workspace: tuple[User, GoogleDriveConnection, BackupSchedule]
) -> None:
    user, _, _ = backup_workspace
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    coordinator = _coordinator(
        session,
        tmp_path,
        now,
        RecordingStore(),
        exporter_failure=RuntimeError("<body>source note content token-value</body>"),
    )

    result = await coordinator.run_backup_for_user(user.id, BackupTrigger.manual)  # type: ignore[arg-type]
    retry = coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]

    assert result.status == BackupStatus.failed
    assert result.failure_message == "Backup failed"
    assert retry.id != result.id
