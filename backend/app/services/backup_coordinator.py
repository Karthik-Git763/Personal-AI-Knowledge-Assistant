from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.database import engine
from app.models.backup import (
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
    GoogleDriveConnection,
    WorkspaceBackup,
)
from app.models.user import User
from app.services.backup_exporter import BackupExporter
from app.services.backup_store import (
    BackupObjectMetadata,
    BackupStore,
    StoredBackup,
    prune_successful_backups,
)
from app.services.google_drive_oauth import GoogleDriveOAuthService
from app.services.google_drive_store import (
    GoogleDriveReauthorizationRequiredError,
    GoogleDriveStore,
)


class BackupVerificationError(RuntimeError):
    """The object returned by storage does not match the exported archive."""


class BackupPreconditionError(RuntimeError):
    """The workspace is not currently eligible for a backup."""


class BackupInterruptedError(RuntimeError):
    """A shutdown or caller cancellation interrupted the backup."""


class BackupExporterProtocol(Protocol):
    def export(self, user: User, destination: Path) -> Any: ...


class BackupStoreFactory(Protocol):
    def __call__(
        self, session: Session, user: User, connection: GoogleDriveConnection
    ) -> BackupStore: ...


class BackupExporterFactory(Protocol):
    def __call__(self, session: Session) -> BackupExporterProtocol: ...


class BackupCoordinator:
    _LOCK_NAMESPACE = 7_327_501

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        lock_session_factory: Callable[[], Session] | None = None,
        exporter_factory: BackupExporterFactory | None = None,
        store_factory: BackupStoreFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        temporary_directory: Path | None = None,
        close_sessions: bool = True,
    ) -> None:
        self.session_factory = session_factory or (lambda: Session(engine))
        self.lock_session_factory = lock_session_factory or self.session_factory
        self.exporter_factory = exporter_factory or (lambda session: BackupExporter(session=session))
        self.store_factory = store_factory or _default_store_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.temporary_directory = Path(temporary_directory or settings.BACKUP_TEMP_DIR)
        self.close_sessions = close_sessions

    def start_backup(self, user_id: int, trigger: BackupTrigger) -> WorkspaceBackup:
        """Create a durable pending operation, collapsing an active user operation."""
        with self._session() as session:
            if not self._try_transaction_lock(session, user_id):
                session.rollback()
                active = self._active_backup(session, user_id)
                if active is None:
                    raise BackupPreconditionError("Backup operation is busy")
                return self._detach(session, active)

            if session.get(User, user_id) is None:
                session.rollback()
                raise BackupPreconditionError("Backup user is unavailable")

            active = self._active_backup(session, user_id)
            if active is not None:
                session.commit()
                return self._detach(session, active)

            backup = WorkspaceBackup(user_id=user_id, trigger=trigger)
            session.add(backup)
            session.commit()
            return self._detach(session, backup)

    async def run_backup(self, backup_id: UUID) -> WorkspaceBackup:
        """Run an already-created backup operation identified by its durable ID."""
        with self._session(self.lock_session_factory) as lock_session:
            with self._session() as session:
                backup = session.exec(
                    select(WorkspaceBackup).where(WorkspaceBackup.backup_id == backup_id)
                ).one_or_none()
                if backup is None:
                    raise BackupPreconditionError("Backup operation is unavailable")
                if backup.status in {BackupStatus.completed, BackupStatus.failed}:
                    return self._detach(session, backup)
                if not self._try_session_lock(lock_session, backup.user_id):
                    return self._detach(session, backup)
                try:
                    return await self._run_locked(session, backup)
                finally:
                    self._release_session_lock(lock_session, backup.user_id)

    async def run_backup_for_user(self, user_id: int, trigger: BackupTrigger) -> WorkspaceBackup:
        backup = self.start_backup(user_id, trigger)
        if backup.backup_id is None:
            raise RuntimeError("Backup operation was not persisted")
        return await self.run_backup(backup.backup_id)

    async def _run_locked(self, session: Session, backup: WorkspaceBackup) -> WorkspaceBackup:
        temporary_path: Path | None = None
        store: BackupStore | None = None
        connection: GoogleDriveConnection | None = None
        now = self.clock()
        try:
            backup.status = BackupStatus.exporting
            backup.started_at = now
            user, connection, schedule = self._requirements(session, backup.user_id)
            schedule.last_attempt_at = now
            session.add_all([backup, schedule])
            session.commit()

            temporary_path = self._create_temporary_directory()
            archive = temporary_path / "workspace-backup.zip"
            export = self.exporter_factory(session).export(user, archive)
            os.chmod(export.path, 0o600)
            if export.manifest.owner_id != user.portable_id:
                raise BackupVerificationError("Backup manifest owner does not match the workspace")
            metadata = BackupObjectMetadata(
                owner_id=export.manifest.owner_id,
                backup_id=export.manifest.backup_id,
                schema_version=export.manifest.schema_version,
                archive_checksum=export.archive_checksum,
                created_at=export.manifest.created_at,
            )
            backup.status = BackupStatus.uploading
            session.add(backup)
            session.commit()
            store = self.store_factory(session, user, connection)
            stored = await store.upload(export.path, metadata)
            self._verify_stored_backup(stored, metadata, export.archive_size)

            completed_at = self.clock()
            backup.remote_file_id = stored.remote_id
            backup.archive_size_bytes = export.archive_size
            backup.checksum = export.archive_checksum
            backup.schema_version = export.manifest.schema_version
            backup.item_counts = export.manifest.counts
            backup.status = BackupStatus.completed
            backup.completed_at = completed_at
            backup.failure_message = None
            schedule.last_success_at = completed_at
            schedule.next_due_at = completed_at + timedelta(hours=24)
            schedule.consecutive_failures = 0
            session.add_all([backup, schedule])
            session.commit()

            await self._run_retention_cleanup(session, backup, store, user.portable_id)
            return self._detach(session, backup)
        except asyncio.CancelledError:
            was_completed = backup.status == BackupStatus.completed
            session.rollback()
            if was_completed:
                raise
            self._mark_failed(session, backup.id, BackupInterruptedError(), connection)
            raise
        except Exception as error:
            session.rollback()
            return self._mark_failed(session, backup.id, error, connection)
        finally:
            if temporary_path is not None:
                shutil.rmtree(temporary_path, ignore_errors=True)
            closer = getattr(store, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()

    async def _run_retention_cleanup(
        self, session: Session, backup: WorkspaceBackup, store: BackupStore, owner_id: UUID
    ) -> None:
        try:
            await prune_successful_backups(store, owner_id=owner_id, keep=5)
        except asyncio.CancelledError:
            self._record_cleanup_warning(session, backup)
            raise
        except Exception:
            self._record_cleanup_warning(session, backup)

    @staticmethod
    def _record_cleanup_warning(session: Session, backup: WorkspaceBackup) -> None:
        try:
            backup.failure_message = "Backup retention cleanup failed"
            session.add(backup)
            session.commit()
        except Exception:
            session.rollback()

    def _requirements(
        self, session: Session, user_id: int
    ) -> tuple[User, GoogleDriveConnection, BackupSchedule]:
        user = session.get(User, user_id)
        connection = session.exec(
            select(GoogleDriveConnection).where(GoogleDriveConnection.user_id == user_id)
        ).one_or_none()
        schedule = session.exec(
            select(BackupSchedule).where(BackupSchedule.user_id == user_id)
        ).one_or_none()
        if user is None or connection is None or schedule is None:
            raise BackupPreconditionError("Backup prerequisites are unavailable")
        if connection.status != DriveConnectionStatus.connected:
            raise BackupPreconditionError("Google Drive authorization is required")
        if not schedule.enabled:
            raise BackupPreconditionError("Backup schedule is disabled")
        return user, connection, schedule

    def _mark_failed(
        self,
        session: Session,
        backup_id: int | None,
        error: Exception,
        connection: GoogleDriveConnection | None,
    ) -> WorkspaceBackup:
        if backup_id is None:
            raise RuntimeError("Backup operation was not persisted") from error
        backup = session.get(WorkspaceBackup, backup_id)
        if backup is None:
            raise RuntimeError("Backup operation is unavailable") from error
        now = self.clock()
        schedule = session.exec(
            select(BackupSchedule).where(BackupSchedule.user_id == backup.user_id)
        ).one_or_none()
        backup.status = BackupStatus.failed
        backup.completed_at = now
        backup.failure_message = self._failure_message(error)
        if schedule is not None:
            schedule.last_attempt_at = now
            schedule.consecutive_failures += 1
            session.add(schedule)
        if isinstance(error, GoogleDriveReauthorizationRequiredError):
            current_connection = connection or session.exec(
                select(GoogleDriveConnection).where(GoogleDriveConnection.user_id == backup.user_id)
            ).one_or_none()
            if current_connection is not None:
                current_connection.status = DriveConnectionStatus.reauthorization_required
                session.add(current_connection)
        session.add(backup)
        session.commit()
        return self._detach(session, backup)

    @staticmethod
    def _verify_stored_backup(
        stored: StoredBackup, metadata: BackupObjectMetadata, archive_size: int
    ) -> None:
        if (
            not stored.completed
            or not stored.remote_id
            or stored.metadata != metadata
            or stored.size != archive_size
        ):
            raise BackupVerificationError("Backup upload verification failed")

    def _create_temporary_directory(self) -> Path:
        self.temporary_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.temporary_directory, 0o700)
        temporary_path = Path(tempfile.mkdtemp(prefix="backup-", dir=self.temporary_directory))
        os.chmod(temporary_path, 0o700)
        return temporary_path

    @staticmethod
    def _failure_message(error: Exception) -> str:
        if isinstance(error, BackupVerificationError):
            return "Backup verification failed"
        if isinstance(error, GoogleDriveReauthorizationRequiredError):
            return "Google Drive reauthorization required"
        if isinstance(error, BackupPreconditionError):
            return "Backup prerequisites are unavailable"
        if isinstance(error, BackupInterruptedError):
            return "Backup interrupted"
        return "Backup failed"

    @staticmethod
    def _active_backup(session: Session, user_id: int) -> WorkspaceBackup | None:
        return session.exec(
            select(WorkspaceBackup)
            .where(
                WorkspaceBackup.user_id == user_id,
                col(WorkspaceBackup.status).in_(
                    [BackupStatus.pending, BackupStatus.exporting, BackupStatus.uploading]
                ),
            )
            .order_by(col(WorkspaceBackup.created_at).desc())
        ).first()

    def _try_transaction_lock(self, session: Session, user_id: int) -> bool:
        return bool(
            session.connection()
            .execute(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :user_id)"),
                {"namespace": self._LOCK_NAMESPACE, "user_id": user_id},
            )
            .scalar_one()
        )

    def _try_session_lock(self, session: Session, user_id: int) -> bool:
        return bool(
            session.connection()
            .execute(
                text("SELECT pg_try_advisory_lock(:namespace, :user_id)"),
                {"namespace": self._LOCK_NAMESPACE, "user_id": user_id},
            )
            .scalar_one()
        )

    def _release_session_lock(self, session: Session, user_id: int) -> None:
        with suppress(Exception):
            session.connection().execute(
                text("SELECT pg_advisory_unlock(:namespace, :user_id)"),
                {"namespace": self._LOCK_NAMESPACE, "user_id": user_id},
            )
        session.rollback()

    @staticmethod
    def _detach(session: Session, backup: WorkspaceBackup) -> WorkspaceBackup:
        session.refresh(backup)
        session.expunge(backup)
        return backup

    @contextmanager
    def _session(
        self, factory: Callable[[], Session] | None = None
    ) -> Generator[Session, None, None]:
        session = (factory or self.session_factory)()
        try:
            yield session
        finally:
            if self.close_sessions:
                session.close()


def _default_store_factory(
    session: Session, user: User, connection: GoogleDriveConnection
) -> GoogleDriveStore:
    return GoogleDriveStore(
        owner_id=user.portable_id,
        connection=connection,
        oauth_service=GoogleDriveOAuthService(session=session),
        session=session,
    )
