from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query, status
from fastapi.responses import RedirectResponse
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.exceptions import AppError
from app.models.backup import (
    BackupOperationKind,
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
    GoogleDriveConnection,
    OAuthState,
    WorkspaceBackup,
)
from app.models.user import User
from app.schemas.backup import (
    DeleteBackupsConfirmationRequest,
    GoogleDriveAuthorizationUrlResponse,
    GoogleDriveBackupStatusResponse,
    RestoreConfirmationRequest,
    RestorePointSummary,
    RestorePreviewResponse,
    WorkspaceBackupResponse,
)
from app.schemas.error import ErrorCode
from app.services.backup_coordinator import BackupCoordinator, BackupPreconditionError
from app.services.backup_store import (
    StoredBackup,
    is_trusted_stored_backup,
    is_valid_stored_backup,
)
from app.services.google_drive_oauth import (
    GoogleDriveOAuthError,
    GoogleDriveOAuthReauthorizationRequiredError,
    GoogleDriveOAuthRetryableError,
    GoogleDriveOAuthService,
    InvalidOAuthState,
    derive_drive_owner_id,
)
from app.services.google_drive_store import (
    GoogleDriveReauthorizationRequiredError,
    GoogleDriveRetryableError,
    GoogleDriveStore,
)

router = APIRouter(prefix="/users/me", tags=["backups"])
_SUPPORTED_RESTORE_SCHEMA_VERSIONS = {1}


class BackupApiErrorCode(StrEnum):
    operation_active = "BACKUP_OPERATION_ACTIVE"
    drive_not_configured = "GOOGLE_DRIVE_NOT_CONFIGURED"
    reauthorization_required = "GOOGLE_DRIVE_REAUTHORIZATION_REQUIRED"
    provider_retryable = "GOOGLE_DRIVE_RETRYABLE"


def _error(message: str, code: BackupApiErrorCode, http_status: int) -> AppError:
    return AppError(message, cast(ErrorCode, code), http_status)


def _require_configuration() -> None:
    if not settings.google_drive_backup_enabled:
        raise _error(
            "Google Drive backups are not configured.",
            BackupApiErrorCode.drive_not_configured,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def _current_user_id(current_user: User) -> int:
    if current_user.id is None:
        raise _error("Authenticated user is unavailable.", BackupApiErrorCode.provider_retryable, 401)
    return current_user.id


def _operation_response(
    backup: WorkspaceBackup, *, restore_eligible: bool = False
) -> WorkspaceBackupResponse:
    return WorkspaceBackupResponse(
        backup_id=backup.backup_id,
        operation_kind=backup.operation_kind,
        source_backup_id=backup.source_backup_id,
        status=backup.status,
        trigger=backup.trigger,
        schema_version=backup.schema_version,
        archive_size_bytes=backup.archive_size_bytes,
        item_counts=backup.item_counts,
        started_at=backup.started_at,
        completed_at=backup.completed_at,
        failure_message=backup.failure_message,
        restore_eligible=restore_eligible,
    )


def _connection(session: SessionDep, user_id: int) -> GoogleDriveConnection:
    connection = session.exec(
        select(GoogleDriveConnection).where(GoogleDriveConnection.user_id == user_id)
    ).one_or_none()
    if connection is None:
        raise _error("Google Drive is not connected.", BackupApiErrorCode.reauthorization_required, 403)
    if connection.status != DriveConnectionStatus.connected:
        raise _error(
            "Google Drive authorization is required.",
            BackupApiErrorCode.reauthorization_required,
            status.HTTP_403_FORBIDDEN,
        )
    return connection


def _store(session: SessionDep, connection: GoogleDriveConnection) -> GoogleDriveStore:
    return GoogleDriveStore(
        connection=connection,
        oauth_service=GoogleDriveOAuthService(session=session),
        session=session,
    )


def _not_found() -> AppError:
    return AppError("Backup was not found.", ErrorCode.NOT_FOUND, status.HTTP_404_NOT_FOUND)


def _map_drive_error(error: Exception) -> AppError:
    if isinstance(error, BackupPreconditionError):
        return AppError(
            "Backup prerequisites are unavailable.",
            ErrorCode.CONFLICT,
            status.HTTP_409_CONFLICT,
        )
    if isinstance(
        error,
        GoogleDriveReauthorizationRequiredError | GoogleDriveOAuthReauthorizationRequiredError,
    ):
        return _error(
            "Google Drive authorization is required.",
            BackupApiErrorCode.reauthorization_required,
            status.HTTP_403_FORBIDDEN,
        )
    if isinstance(error, GoogleDriveRetryableError | GoogleDriveOAuthRetryableError):
        return _error(
            "Google Drive is temporarily unavailable. Please retry.",
            BackupApiErrorCode.provider_retryable,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if isinstance(error, GoogleDriveOAuthError):
        return _error(
            "Google Drive request could not be completed.",
            BackupApiErrorCode.provider_retryable,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    raise error


async def _close_store(store: GoogleDriveStore) -> None:
    await store.aclose()


async def _trusted_remote_backups(
    session: SessionDep, current_user: User, connection: GoogleDriveConnection
) -> list[StoredBackup]:
    store = _store(session, connection)
    try:
        owner_id = derive_drive_owner_id(connection.google_subject)
        remote_backups = await store.list(owner_id)
        return [backup for backup in remote_backups if is_valid_stored_backup(backup, owner_id)]
    except Exception as error:
        raise _map_drive_error(error) from error
    finally:
        await _close_store(store)


def _matching_local_backup(
    session: SessionDep, user_id: int, remote: StoredBackup
) -> WorkspaceBackup | None:
    return session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user_id,
            WorkspaceBackup.remote_file_id == remote.remote_id,
            WorkspaceBackup.status == BackupStatus.completed,
            WorkspaceBackup.operation_kind == BackupOperationKind.snapshot,
        )
    ).one_or_none()


def _restore_eligible(backup: WorkspaceBackup, remote: StoredBackup) -> bool:
    return (
        backup.operation_kind == BackupOperationKind.snapshot
        and backup.status == BackupStatus.completed
        and backup.schema_version in _SUPPORTED_RESTORE_SCHEMA_VERSIONS
        and remote.completed
        and remote.metadata.schema_version == backup.schema_version
        and remote.metadata.archive_checksum == backup.checksum
        and remote.size == backup.archive_size_bytes
    )


def _active_operation(session: SessionDep, user_id: int) -> WorkspaceBackup | None:
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


@router.post("/google-drive/connect", response_model=GoogleDriveAuthorizationUrlResponse)
def connect_google_drive(session: SessionDep, current_user: CurrentUser) -> GoogleDriveAuthorizationUrlResponse:
    _require_configuration()
    try:
        url = GoogleDriveOAuthService(session=session).create_authorization_url(
            _current_user_id(current_user)
        )
    except Exception as error:
        raise _map_drive_error(error) from error
    return GoogleDriveAuthorizationUrlResponse(authorization_url=url)


@router.get("/google-drive/callback", include_in_schema=False)
async def google_drive_callback(
    session: SessionDep, state: str | None = Query(default=None), code: str | None = Query(default=None)
) -> RedirectResponse:
    frontend_settings = f"{settings.FRONTEND_HOST.rstrip('/')}/settings"
    if not settings.google_drive_backup_enabled:
        return RedirectResponse(f"{frontend_settings}?drive=not_configured", status_code=303)
    if not state or not code:
        return RedirectResponse(f"{frontend_settings}?drive=invalid_state", status_code=303)

    state_hash = hashlib.sha256(state.encode()).hexdigest()
    state_record = session.exec(
        select(OAuthState).where(OAuthState.state_hash == state_hash)
    ).one_or_none()
    if (
        state_record is None
        or state_record.consumed_at is not None
        or state_record.expires_at <= datetime.now(UTC)
    ):
        return RedirectResponse(f"{frontend_settings}?drive=invalid_state", status_code=303)
    try:
        service = GoogleDriveOAuthService(session=session)
        await service.complete_authorization(user_id=state_record.user_id, state=state, code=code)
        schedule = session.exec(
            select(BackupSchedule).where(BackupSchedule.user_id == state_record.user_id)
        ).one_or_none()
        if schedule is None:
            schedule = BackupSchedule(
                user_id=state_record.user_id,
                enabled=True,
                interval_hours=24,
                next_due_at=datetime.now(UTC) + timedelta(hours=24),
            )
        else:
            schedule.enabled = True
            schedule.interval_hours = 24
            if schedule.next_due_at is None:
                schedule.next_due_at = datetime.now(UTC) + timedelta(hours=24)
        session.add(schedule)
        session.commit()
    except (InvalidOAuthState, GoogleDriveOAuthError):
        return RedirectResponse(f"{frontend_settings}?drive=authorization_failed", status_code=303)
    except Exception:
        return RedirectResponse(f"{frontend_settings}?drive=authorization_failed", status_code=303)
    return RedirectResponse(f"{frontend_settings}?drive=connected", status_code=303)


@router.get("/google-drive/status", response_model=GoogleDriveBackupStatusResponse)
async def google_drive_status(
    background_tasks: BackgroundTasks, session: SessionDep, current_user: CurrentUser
) -> GoogleDriveBackupStatusResponse:
    _require_configuration()
    user_id = _current_user_id(current_user)
    connection = session.exec(
        select(GoogleDriveConnection).where(GoogleDriveConnection.user_id == user_id)
    ).one_or_none()
    schedule = session.exec(select(BackupSchedule).where(BackupSchedule.user_id == user_id)).one_or_none()
    operation = _active_operation(session, user_id)
    if (
        connection is not None
        and connection.status == DriveConnectionStatus.connected
        and schedule is not None
        and schedule.enabled
        and schedule.next_due_at is not None
        and schedule.next_due_at <= datetime.now(UTC)
        and operation is None
    ):
        coordinator = BackupCoordinator()
        try:
            operation = coordinator.start_backup(user_id, BackupTrigger.scheduled)
            background_tasks.add_task(coordinator.run_backup, operation.backup_id)
        except BackupPreconditionError:
            operation = _active_operation(session, user_id)

    restore_points: list[RestorePointSummary] = []
    if connection is not None and connection.status == DriveConnectionStatus.connected:
        remote_backups = await _trusted_remote_backups(session, current_user, connection)
        for remote in sorted(remote_backups, key=lambda backup: backup.created_at, reverse=True)[:5]:
            local = _matching_local_backup(session, user_id, remote)
            if local is not None:
                restore_points.append(
                    RestorePointSummary(
                        backup_id=local.backup_id,
                        schema_version=remote.metadata.schema_version,
                        archive_size_bytes=remote.size,
                        created_at=remote.metadata.created_at,
                        restore_eligible=_restore_eligible(local, remote),
                    )
                )

    return GoogleDriveBackupStatusResponse(
        configured=True,
        enabled=bool(schedule and schedule.enabled),
        connection_status=connection.status if connection is not None else None,
        google_email=connection.google_email if connection is not None else None,
        last_attempt_at=schedule.last_attempt_at if schedule is not None else None,
        last_success_at=schedule.last_success_at if schedule is not None else None,
        next_due_at=schedule.next_due_at if schedule is not None else None,
        consecutive_failures=schedule.consecutive_failures if schedule is not None else 0,
        active_operation=_operation_response(operation) if operation is not None else None,
        restore_points=restore_points,
    )


@router.delete("/google-drive")
async def disconnect_google_drive(session: SessionDep, current_user: CurrentUser) -> dict[str, str]:
    _require_configuration()
    connection = _connection(session, _current_user_id(current_user))
    try:
        await GoogleDriveOAuthService(session=session).disconnect(connection)
    except Exception as error:
        raise _map_drive_error(error) from error
    return {"status": "disconnected"}


@router.post("/google-drive/delete-backups")
async def delete_google_drive_backups(
    body: DeleteBackupsConfirmationRequest, session: SessionDep, current_user: CurrentUser
) -> dict[str, int]:
    _require_configuration()
    connection = _connection(session, _current_user_id(current_user))
    store = _store(session, connection)
    try:
        owner_id = derive_drive_owner_id(connection.google_subject)
        backups = await store.list(owner_id)
        trusted = [backup for backup in backups if is_trusted_stored_backup(backup, owner_id)]
        for backup in trusted:
            store.authorize_backup(backup)
            await store.delete(backup.remote_id)
    except Exception as error:
        raise _map_drive_error(error) from error
    finally:
        await _close_store(store)
    return {"deleted": len(trusted)}


@router.post("/backups", response_model=WorkspaceBackupResponse, status_code=status.HTTP_202_ACCEPTED)
def create_backup(
    background_tasks: BackgroundTasks, session: SessionDep, current_user: CurrentUser
) -> WorkspaceBackupResponse:
    _require_configuration()
    _connection(session, _current_user_id(current_user))
    coordinator = BackupCoordinator()
    try:
        operation = coordinator.start_manual_backup(_current_user_id(current_user))
    except BackupPreconditionError as error:
        raise _error(
            "A backup operation is already active.",
            BackupApiErrorCode.operation_active,
            status.HTTP_409_CONFLICT,
        ) from error
    background_tasks.add_task(coordinator.run_backup, operation.backup_id)
    return _operation_response(operation)


@router.get("/backups", response_model=list[WorkspaceBackupResponse])
async def list_backups(session: SessionDep, current_user: CurrentUser) -> list[WorkspaceBackupResponse]:
    _require_configuration()
    user_id = _current_user_id(current_user)
    connection = _connection(session, user_id)
    remote_backups = await _trusted_remote_backups(session, current_user, connection)
    local_by_remote = {
        backup.remote_file_id: backup
        for backup in session.exec(
            select(WorkspaceBackup).where(
                WorkspaceBackup.user_id == user_id,
                WorkspaceBackup.status == BackupStatus.completed,
                WorkspaceBackup.operation_kind == BackupOperationKind.snapshot,
            )
        ).all()
        if backup.remote_file_id is not None
    }
    return [
        _operation_response(
            local_by_remote[remote.remote_id],
            restore_eligible=_restore_eligible(local_by_remote[remote.remote_id], remote),
        )
        for remote in sorted(remote_backups, key=lambda backup: backup.created_at, reverse=True)
        if remote.remote_id in local_by_remote
    ]


@router.get("/backups/{backup_id}/preview", response_model=RestorePreviewResponse)
async def preview_backup(
    backup_id: UUID, session: SessionDep, current_user: CurrentUser
) -> RestorePreviewResponse:
    _require_configuration()
    user_id = _current_user_id(current_user)
    source = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.backup_id == backup_id,
            WorkspaceBackup.user_id == user_id,
            WorkspaceBackup.status == BackupStatus.completed,
            WorkspaceBackup.operation_kind == BackupOperationKind.snapshot,
        )
    ).one_or_none()
    if source is None:
        raise _not_found()
    _connection(session, user_id)
    try:
        preview = await BackupCoordinator().preview_restore(user_id, backup_id)
    except Exception as error:
        raise _map_drive_error(error) from error
    return RestorePreviewResponse(
        backup_id=backup_id,
        schema_version=preview.schema_version,
        item_counts=preview.item_counts,
        archive_size_bytes=preview.archive_size_bytes,
    )


@router.post(
    "/backups/{backup_id}/restore",
    response_model=WorkspaceBackupResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def restore_backup(
    backup_id: UUID,
    body: RestoreConfirmationRequest,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    current_user: CurrentUser,
) -> WorkspaceBackupResponse:
    _require_configuration()
    user_id = _current_user_id(current_user)
    source = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.backup_id == backup_id,
            WorkspaceBackup.user_id == user_id,
            WorkspaceBackup.status == BackupStatus.completed,
            WorkspaceBackup.operation_kind == BackupOperationKind.snapshot,
        )
    ).one_or_none()
    if source is None:
        raise _not_found()
    _connection(session, user_id)
    coordinator = BackupCoordinator()
    try:
        operation = coordinator.start_restore(
            user_id, backup_id, body.confirmation
        )
    except BackupPreconditionError as error:
        raise _error(
            "A backup operation is already active.",
            BackupApiErrorCode.operation_active,
            status.HTTP_409_CONFLICT,
        ) from error
    background_tasks.add_task(coordinator.run_restore, operation.backup_id)
    return _operation_response(operation)


@router.get("/backups/operations/{operation_id}", response_model=WorkspaceBackupResponse)
def get_operation(
    operation_id: UUID, session: SessionDep, current_user: CurrentUser
) -> WorkspaceBackupResponse:
    operation = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.backup_id == operation_id,
            WorkspaceBackup.user_id == _current_user_id(current_user),
        )
    ).one_or_none()
    if operation is None:
        raise _not_found()
    return _operation_response(operation)
