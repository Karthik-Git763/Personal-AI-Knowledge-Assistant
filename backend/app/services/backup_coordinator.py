from __future__ import annotations

import asyncio
import logging
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
    BackupOperationKind,
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
    GoogleDriveConnection,
    WorkspaceBackup,
)
from app.models.user import User
from app.schemas.backup import BackupOperationStatusResponse, BackupPreview
from app.services.backup_archive import sha256_file
from app.services.backup_exporter import BackupExporter
from app.services.backup_importer import (
    BackupImporter,
    BackupRestoreFailed,
    RestoreResult,
)
from app.services.backup_store import (
    MINIMUM_BACKUP_ARCHIVE_SIZE,
    BackupObjectMetadata,
    BackupStore,
    StoredBackup,
    is_valid_stored_backup,
    prune_successful_backups,
)
from app.services.document_service import process_existing_document
from app.services.google_drive_oauth import GoogleDriveOAuthService, derive_drive_owner_id
from app.services.google_drive_store import (
    GoogleDriveReauthorizationRequiredError,
    GoogleDriveStore,
)

logger = logging.getLogger(__name__)


class BackupVerificationError(RuntimeError):
    """The object returned by storage does not match the exported archive."""


class BackupPreconditionError(RuntimeError):
    """The workspace is not currently eligible for a backup."""


class BackupInterruptedError(RuntimeError):
    """A shutdown or caller cancellation interrupted the backup."""


class BackupExporterProtocol(Protocol):
    def export(
        self, user: User, destination: Path, *, backup_id: UUID | None = None
    ) -> Any: ...


class BackupStoreFactory(Protocol):
    def __call__(
        self, session: Session, user: User, connection: GoogleDriveConnection
    ) -> BackupStore: ...


class BackupExporterFactory(Protocol):
    def __call__(self, session: Session) -> BackupExporterProtocol: ...


class BackupImporterProtocol(Protocol):
    def preview(
        self,
        path: Path,
        expected_workspace_owner_id: UUID,
        expected_archive_backup_id: UUID | None = None,
    ) -> BackupPreview: ...

    def restore(
        self,
        path: Path,
        user: User,
        expected_workspace_owner_id: UUID,
        *,
        expected_archive_backup_id: UUID | None = None,
        operation: WorkspaceBackup,
        completed_at: datetime,
    ) -> RestoreResult: ...


class BackupImporterFactory(Protocol):
    def __call__(self, session: Session) -> BackupImporterProtocol: ...


class RestoredDocumentProcessor(Protocol):
    async def __call__(
        self, session: Session, user_id: int, document_id: int
    ) -> Any: ...


class BackupCoordinator:
    _LOCK_NAMESPACE = 7_327_501

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
        lock_session_factory: Callable[[], Session] | None = None,
        exporter_factory: BackupExporterFactory | None = None,
        importer_factory: BackupImporterFactory | None = None,
        store_factory: BackupStoreFactory | None = None,
        document_processor: RestoredDocumentProcessor | None = None,
        clock: Callable[[], datetime] | None = None,
        temporary_directory: Path | None = None,
        close_sessions: bool = True,
    ) -> None:
        self.session_factory = session_factory or (lambda: Session(engine))
        self.lock_session_factory = lock_session_factory or self.session_factory
        self.exporter_factory = exporter_factory or (lambda session: BackupExporter(session=session))
        self.importer_factory = importer_factory or (
            lambda session: BackupImporter(session=session)
        )
        self.store_factory = store_factory or _default_store_factory
        self.document_processor = document_processor or process_existing_document
        self.clock = clock or (lambda: datetime.now(UTC))
        self.temporary_directory = Path(temporary_directory or settings.BACKUP_TEMP_DIR)
        self.close_sessions = close_sessions

    def start_backup(self, user_id: int, trigger: BackupTrigger) -> WorkspaceBackup:
        """Create a durable pending operation, collapsing an active user operation."""
        return self._start_backup(user_id, trigger, collapse_active=True)

    def start_manual_backup(self, user_id: int) -> WorkspaceBackup:
        """Create a manual backup or reject it when any operation is active."""
        return self._start_backup(user_id, BackupTrigger.manual, collapse_active=False)

    def _start_backup(
        self, user_id: int, trigger: BackupTrigger, *, collapse_active: bool
    ) -> WorkspaceBackup:
        with self._session() as session:
            if not self._try_transaction_lock(session, user_id):
                session.rollback()
                active = self._active_backup(session, user_id)
                if active is None or not collapse_active:
                    raise BackupPreconditionError("Backup operation already active")
                return self._detach(session, active)

            if session.get(User, user_id) is None:
                session.rollback()
                raise BackupPreconditionError("Backup user is unavailable")

            active = self._active_backup(session, user_id)
            if active is not None:
                if not collapse_active or active.operation_kind != BackupOperationKind.snapshot:
                    session.rollback()
                    raise BackupPreconditionError("Backup operation already active")
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
                if backup.operation_kind != BackupOperationKind.snapshot:
                    raise BackupPreconditionError("Operation is not a backup snapshot")
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

    async def preview_restore(
        self, user_id: int, backup_id: UUID
    ) -> BackupPreview:
        """Validate restore metadata without mutating workspace rows or files.

        Storage access may persist valid OAuth token-refresh bookkeeping on the
        Google Drive connection.
        """
        temporary_path: Path | None = None
        store: BackupStore | None = None
        try:
            with self._session() as session:
                user, connection, source = self._restore_requirements(
                    session, user_id, backup_id
                )
                drive_owner_id = derive_drive_owner_id(connection.google_subject)
                store = self.store_factory(session, user, connection)
                stored = await self._resolve_source_backup(
                    store, source, drive_owner_id
                )
                temporary_path = self._create_temporary_directory()
                archive = await self._download_verified_source(
                    store, stored, source, temporary_path
                )
                return self.importer_factory(session).preview(
                    archive,
                    expected_workspace_owner_id=stored.metadata.workspace_owner_id,
                    expected_archive_backup_id=stored.metadata.backup_id,
                )
        finally:
            if temporary_path is not None:
                shutil.rmtree(temporary_path, ignore_errors=True)
            await self._close_store(store)

    def start_restore(
        self, user_id: int, backup_id: UUID, confirmation: str
    ) -> WorkspaceBackup:
        """Persist a restore operation before background execution begins."""
        if confirmation != "RESTORE":
            raise BackupPreconditionError("Exact restore confirmation is required")

        with self._session() as session:
            if not self._try_transaction_lock(session, user_id):
                session.rollback()
                raise BackupPreconditionError("Backup operation is busy")
            if self._active_backup(session, user_id) is not None:
                session.rollback()
                raise BackupPreconditionError("Backup operation is busy")
            _, _, source = self._restore_requirements(session, user_id, backup_id)
            restore_operation = WorkspaceBackup(
                user_id=user_id,
                operation_kind=BackupOperationKind.restore,
                source_backup_id=source.backup_id,
                status=BackupStatus.pending,
                trigger=BackupTrigger.manual,
                started_at=self.clock(),
            )
            session.add(restore_operation)
            session.commit()
            return self._detach(session, restore_operation)

    async def restore(
        self, user_id: int, backup_id: UUID, confirmation: str
    ) -> BackupOperationStatusResponse:
        operation = self.start_restore(user_id, backup_id, confirmation)
        if operation.backup_id is None:
            raise RuntimeError("Restore operation was not persisted")
        return await self.run_restore(operation.backup_id)

    async def run_restore(
        self, restore_backup_id: UUID
    ) -> BackupOperationStatusResponse:
        """Run an existing restore operation while preserving restore safeguards."""

        restore_operation_id: int | None = None
        locked_user_id: int | None = None
        temporary_path: Path | None = None
        store: BackupStore | None = None
        with self._session(self.lock_session_factory) as lock_session:
            try:
                with self._session() as session:
                    try:
                        restore_operation = session.exec(
                            select(WorkspaceBackup).where(
                                WorkspaceBackup.backup_id == restore_backup_id
                            )
                        ).one_or_none()
                        if (
                            restore_operation is None
                            or restore_operation.operation_kind
                            != BackupOperationKind.restore
                        ):
                            raise BackupPreconditionError("Restore operation is unavailable")
                        if restore_operation.status in {
                            BackupStatus.completed,
                            BackupStatus.failed,
                        }:
                            return BackupOperationStatusResponse(
                                status=restore_operation.status,
                                backup_id=restore_operation.backup_id,
                            )
                        user_id = restore_operation.user_id
                        restore_operation_id = restore_operation.id
                        if not self._try_session_lock(lock_session, user_id):
                            raise BackupPreconditionError("Backup operation is busy")
                        locked_user_id = user_id
                        if restore_operation.source_backup_id is None:
                            raise BackupPreconditionError("Restore source is unavailable")
                        user, connection, source = self._restore_requirements(
                            session, user_id, restore_operation.source_backup_id
                        )

                        drive_owner_id = derive_drive_owner_id(
                            connection.google_subject
                        )
                        store = self.store_factory(session, user, connection)
                        stored = await self._resolve_source_backup(
                            store, source, drive_owner_id
                        )
                        temporary_path = self._create_temporary_directory()
                        archive = await self._download_verified_source(
                            store, stored, source, temporary_path
                        )
                        self.importer_factory(session).preview(
                            archive,
                            expected_workspace_owner_id=(
                                stored.metadata.workspace_owner_id
                            ),
                            expected_archive_backup_id=(
                                stored.metadata.backup_id
                            ),
                        )

                        safety_backup = WorkspaceBackup(
                            user_id=user_id,
                            operation_kind=BackupOperationKind.snapshot,
                            trigger=BackupTrigger.manual,
                        )
                        session.add(safety_backup)
                        session.commit()
                        safety_result = await self._run_locked(
                            session, safety_backup, run_retention=False
                        )
                        if safety_result.status != BackupStatus.completed:
                            raise BackupRestoreFailed(
                                "Safety backup did not complete"
                            )

                        operation = self._restore_operation(
                            session, restore_operation_id
                        )
                        restore_result = self.importer_factory(session).restore(
                            archive,
                            user,
                            expected_workspace_owner_id=(
                                stored.metadata.workspace_owner_id
                            ),
                            expected_archive_backup_id=(
                                stored.metadata.backup_id
                            ),
                            operation=operation,
                            completed_at=self.clock(),
                        )
                        await self._reprocess_restored_documents(
                            session,
                            user_id,
                            restore_result.reprocessing_document_ids,
                        )
                        return BackupOperationStatusResponse(
                            status=operation.status,
                            backup_id=operation.backup_id,
                        )
                    except asyncio.CancelledError:
                        session.rollback()
                        self._mark_restore_failed(
                            session, restore_operation_id
                        )
                        raise
                    except Exception as error:
                        session.rollback()
                        self._mark_restore_failed(
                            session, restore_operation_id
                        )
                        if isinstance(error, BackupRestoreFailed):
                            raise
                        raise BackupRestoreFailed(
                            "Workspace restore failed"
                        ) from error
                    finally:
                        if temporary_path is not None:
                            shutil.rmtree(
                                temporary_path, ignore_errors=True
                            )
                        await self._close_store(store)
            finally:
                if locked_user_id is not None:
                    self._release_session_lock(lock_session, locked_user_id)

    async def _reprocess_restored_documents(
        self,
        session: Session,
        user_id: int,
        document_ids: list[int],
    ) -> None:
        for document_id in document_ids:
            try:
                await self.document_processor(session, user_id, document_id)
            except Exception:
                logger.exception(
                    "Restored document reprocessing failed",
                    extra={"document_id": document_id, "user_id": user_id},
                )

    async def _run_locked(
        self,
        session: Session,
        backup: WorkspaceBackup,
        *,
        run_retention: bool = True,
    ) -> WorkspaceBackup:
        if backup.operation_kind != BackupOperationKind.snapshot:
            raise BackupPreconditionError("Operation is not a backup snapshot")
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
            export = self.exporter_factory(session).export(
                user,
                archive,
                backup_id=backup.backup_id,
            )
            os.chmod(export.path, 0o600)
            if export.manifest.owner_id != user.portable_id:
                raise BackupVerificationError("Backup manifest owner does not match the workspace")
            drive_owner_id = derive_drive_owner_id(connection.google_subject)
            metadata = BackupObjectMetadata(
                drive_owner_id=drive_owner_id,
                workspace_owner_id=export.manifest.owner_id,
                backup_id=export.manifest.backup_id,
                schema_version=export.manifest.schema_version,
                archive_checksum=export.archive_checksum,
                created_at=export.manifest.created_at,
                app_version=export.manifest.app_version,
                item_counts=dict(export.manifest.counts),
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
            schedule.interval_hours = settings.BACKUP_INTERVAL_HOURS
            schedule.next_due_at = completed_at + timedelta(
                hours=settings.BACKUP_INTERVAL_HOURS
            )
            schedule.consecutive_failures = 0
            session.add_all([backup, schedule])
            session.commit()

            if run_retention:
                await self._run_retention_cleanup(
                    session, backup, store, drive_owner_id
                )
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
        self, session: Session, backup: WorkspaceBackup, store: BackupStore, drive_owner_id: UUID
    ) -> None:
        try:
            await prune_successful_backups(
                store,
                drive_owner_id=drive_owner_id,
                keep=settings.BACKUP_RETENTION_COUNT,
            )
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

    def _restore_requirements(
        self, session: Session, user_id: int, backup_id: UUID
    ) -> tuple[User, GoogleDriveConnection, WorkspaceBackup]:
        user = session.get(User, user_id)
        connection = session.exec(
            select(GoogleDriveConnection).where(
                GoogleDriveConnection.user_id == user_id
            )
        ).one_or_none()
        source = session.exec(
            select(WorkspaceBackup).where(
                WorkspaceBackup.user_id == user_id,
                WorkspaceBackup.backup_id == backup_id,
            )
        ).one_or_none()
        if user is None or connection is None or source is None:
            raise BackupPreconditionError(
                "Restore prerequisites are unavailable"
            )
        if connection.status != DriveConnectionStatus.connected:
            raise BackupPreconditionError(
                "Google Drive authorization is required"
            )
        if (
            source.operation_kind != BackupOperationKind.snapshot
            or source.status != BackupStatus.completed
            or not source.remote_file_id
            or not source.checksum
        ):
            raise BackupPreconditionError(
                "Selected backup is not restorable"
            )
        return user, connection, source

    async def _resolve_source_backup(
        self,
        store: BackupStore,
        source: WorkspaceBackup,
        drive_owner_id: UUID,
    ) -> StoredBackup:
        backups = await store.list(drive_owner_id)
        stored = next(
            (
                backup
                for backup in backups
                if backup.remote_id == source.remote_file_id
            ),
            None,
        )
        if (
            stored is None
            or not is_valid_stored_backup(
                stored,
                drive_owner_id,
                settings.BACKUP_MAX_ARCHIVE_SIZE,
            )
            or stored.metadata.backup_id != source.backup_id
            or stored.metadata.archive_checksum != source.checksum
            or stored.metadata.schema_version != source.schema_version
            or (
                source.archive_size_bytes is not None
                and stored.size != source.archive_size_bytes
            )
        ):
            raise BackupPreconditionError(
                "Selected backup could not be verified in Google Drive"
            )
        return stored

    async def _download_verified_source(
        self,
        store: BackupStore,
        stored: StoredBackup,
        source: WorkspaceBackup,
        temporary_path: Path,
    ) -> Path:
        destination = temporary_path / "restore-source.zip"
        downloaded = await store.download(stored.remote_id, destination)
        try:
            if Path(downloaded).resolve() != destination.resolve():
                raise BackupPreconditionError(
                    "Backup download destination is invalid"
                )
            size = destination.stat().st_size
        except OSError as error:
            raise BackupPreconditionError(
                "Backup download is unavailable"
            ) from error
        if (
            size < MINIMUM_BACKUP_ARCHIVE_SIZE
            or size > settings.BACKUP_MAX_ARCHIVE_SIZE
            or size != stored.size
            or (
                source.archive_size_bytes is not None
                and size != source.archive_size_bytes
            )
            or sha256_file(destination)
            != stored.metadata.archive_checksum
        ):
            raise BackupPreconditionError(
                "Downloaded backup verification failed"
            )
        return destination

    @staticmethod
    async def _close_store(store: BackupStore | None) -> None:
        if store is None:
            return
        closer = getattr(store, "aclose", None)
        if closer is not None:
            with suppress(Exception):
                await closer()

    @staticmethod
    def _restore_operation(
        session: Session, operation_id: int | None
    ) -> WorkspaceBackup:
        if operation_id is None:
            raise RuntimeError("Restore operation was not persisted")
        operation = session.get(WorkspaceBackup, operation_id)
        if operation is None:
            raise RuntimeError("Restore operation is unavailable")
        return operation

    def _mark_restore_failed(
        self, session: Session, operation_id: int | None
    ) -> None:
        if operation_id is None:
            return
        operation = session.get(WorkspaceBackup, operation_id)
        if operation is None:
            return
        if operation.status == BackupStatus.completed:
            return
        operation.status = BackupStatus.failed
        operation.completed_at = self.clock()
        operation.failure_message = "Workspace restore failed"
        session.add(operation)
        session.commit()

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
        connection=connection,
        oauth_service=GoogleDriveOAuthService(session=session),
        session=session,
    )
