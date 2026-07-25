import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import Session

from app.core.config import Settings
from app.core.security import encrypt_provider_token
from app.models.backup import BackupStatus, GoogleDriveConnection, WorkspaceBackup
from app.models.user import User
from app.services.backup_store import BackupObjectMetadata, StoredBackup
from app.services.google_drive_oauth import GoogleDriveOAuthService, derive_drive_owner_id
from app.services.google_drive_store import (
    GoogleDriveMalformedPayloadError,
    GoogleDriveReauthorizationRequiredError,
    GoogleDriveRetryableError,
    GoogleDriveStore,
    InvalidGoogleDriveRemoteIdError,
)

ARCHIVE_BYTES = b"cognolith-backup-contents"
CHECKSUM = "a" * 64
REMOTE_ID = "drive_file_12345"
SERVER_CREATED_AT = datetime(2026, 7, 24, 12, 5, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        SECRET_KEY="test-secret",
        SMTP_HOST="mailpit",
        SMTP_PORT=1025,
        SMTP_TLS=False,
        EMAILS_FROM_EMAIL="no-reply@example.com",
        GOOGLE_DRIVE_CLIENT_ID="google-client-id",
        GOOGLE_DRIVE_CLIENT_SECRET="google-client-secret",
        GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        BACKUP_MAX_ARCHIVE_SIZE=64,
    )


@pytest.fixture(autouse=True)
def configured_token_encryption(monkeypatch: pytest.MonkeyPatch) -> Settings:
    configured_settings = _settings()
    monkeypatch.setattr("app.core.security.settings", configured_settings)
    return configured_settings


def _connection(
    session: Session,
    settings: Settings,
    *,
    email: str = "drive-store@example.com",
    google_subject: str = "google-subject",
) -> tuple[UUID, GoogleDriveConnection]:
    user = User(email=email, hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=encrypt_provider_token(
            "refresh-secret", encryption_key=settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY
        ),
        google_subject=google_subject,
        google_email=user.email,
        granted_scopes=[GoogleDriveOAuthService.REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()
    return user.portable_id, connection


def _metadata(
    drive_owner_id: UUID,
    workspace_owner_id: UUID | None = None,
    *,
    include_optional: bool = True,
) -> BackupObjectMetadata:
    return BackupObjectMetadata(
        drive_owner_id=drive_owner_id,
        workspace_owner_id=workspace_owner_id or uuid4(),
        backup_id=uuid4(),
        schema_version=1,
        archive_checksum=CHECKSUM,
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        app_version="0.1.0" if include_optional else None,
        item_counts={"notes": 4, "documents": 2} if include_optional else {},
    )


def _drive_file(
    metadata: BackupObjectMetadata,
    *,
    completed: bool = True,
    remote_id: str = REMOTE_ID,
    parents: list[str] | None = None,
    server_created_at: datetime = SERVER_CREATED_AT,
) -> dict[str, Any]:
    app_properties = {
        "cognolith": "backup",
        "drive_owner_id": str(metadata.drive_owner_id),
        "workspace_owner_id": str(metadata.workspace_owner_id),
        "backup_id": str(metadata.backup_id),
        "schema_version": str(metadata.schema_version),
        "archive_checksum": metadata.archive_checksum,
        "created_at": metadata.created_at.isoformat().replace("+00:00", "Z"),
        "completed": str(completed).lower(),
    }
    if metadata.app_version is not None:
        app_properties["app_version"] = metadata.app_version
    if metadata.item_counts:
        app_properties["item_counts"] = '{"d":2,"n":4}'
    return {
        "id": remote_id,
        "name": f"cognolith-{metadata.backup_id}.zip",
        "size": str(len(ARCHIVE_BYTES)),
        "createdTime": server_created_at.isoformat().replace("+00:00", "Z"),
        "parents": parents if parents is not None else ["app_data_folder_id_12345"],
        "appProperties": app_properties,
    }


def _stored_backup(metadata: BackupObjectMetadata) -> StoredBackup:
    return StoredBackup(
        remote_id=REMOTE_ID,
        name=f"cognolith-{metadata.backup_id}.zip",
        size=len(ARCHIVE_BYTES),
        created_at=metadata.created_at,
        metadata=metadata,
        completed=True,
    )


async def _store(
    session: Session,
    settings: Settings,
    handler: httpx.MockTransport | Any,
    *,
    with_session: bool = False,
) -> tuple[GoogleDriveStore, UUID]:
    _, connection = _connection(session, settings)
    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    oauth_service = GoogleDriveOAuthService(session=session, client=client, settings=settings)
    store_kwargs: dict[str, Session] = {"session": session} if with_session else {}
    store = GoogleDriveStore(
        connection=connection,
        oauth_service=oauth_service,
        client=client,
        settings=settings,
        **store_kwargs,
    )
    return store, store.drive_owner_id


def _token_response(request: httpx.Request) -> httpx.Response:
    assert request.url == GoogleDriveOAuthService.TOKEN_URL
    return httpx.Response(200, json={"access_token": "access-secret", "expires_in": 3600})


@pytest.mark.asyncio
async def test_upload_uses_resumable_appdata_and_marks_validated_file_complete(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    archive = tmp_path / "archive.zip"
    archive.write_bytes(ARCHIVE_BYTES)
    requests: list[httpx.Request] = []
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metadata
        requests.append(request)
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        if request.method == "POST" and request.url.path.endswith("/upload/drive/v3/files"):
            body = json.loads(request.content)
            assert metadata is not None
            assert body["parents"] == ["appDataFolder"]
            assert body["appProperties"]["completed"] == "false"
            assert body["appProperties"]["drive_owner_id"] == str(metadata.drive_owner_id)
            assert body["appProperties"]["workspace_owner_id"] == str(metadata.workspace_owner_id)
            assert "google-subject" not in body["appProperties"].values()
            assert request.url.params["uploadType"] == "resumable"
            return httpx.Response(200, headers={"Location": "https://upload.example/session"})
        if request.method == "PUT" and request.url == "https://upload.example/session":
            assert request.headers["Content-Type"] == "application/zip"
            assert request.content == ARCHIVE_BYTES
            assert metadata is not None
            return httpx.Response(200, json=_drive_file(metadata, completed=False))
        if request.method == "PATCH" and request.url.path.endswith(f"/drive/v3/files/{REMOTE_ID}"):
            assert json.loads(request.content)["appProperties"]["completed"] == "true"
            assert metadata is not None
            return httpx.Response(200, json=_drive_file(metadata, completed=True))
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        uploaded = await store.upload(archive, metadata)
    finally:
        await store.aclose()

    assert uploaded.remote_id == REMOTE_ID
    assert uploaded.completed is True
    assert uploaded.created_at == metadata.created_at
    assert [request.method for request in requests] == ["POST", "POST", "PUT", "PATCH"]


@pytest.mark.asyncio
async def test_list_uses_escaped_exact_owner_query_and_rejects_malformed_payload(
    session: Session, configured_token_encryption: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert request.method == "GET"
        query = request.url.params["q"]
        assert "trashed = false" in query
        assert "appProperties has" in query
        assert "drive_owner_id" in query
        assert "workspace_owner_id" not in query
        return httpx.Response(200, json={"files": [{"id": REMOTE_ID}]})

    store, owner_id = await _store(session, configured_token_encryption, handler)
    try:
        with pytest.raises(GoogleDriveMalformedPayloadError):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_list_returns_only_valid_owner_backups_and_authorizes_remote_id_for_delete(
    session: Session, configured_token_encryption: Settings
) -> None:
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        if request.method == "GET":
            assert metadata is not None
            return httpx.Response(200, json={"files": [_drive_file(metadata)]})
        if request.method == "DELETE":
            assert request.url.path.endswith(REMOTE_ID)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        listed = await store.list(owner_id)
        await store.delete(listed[0].remote_id)
    finally:
        await store.aclose()

    assert listed[0].metadata == metadata
    assert listed[0].created_at == metadata.created_at


@pytest.mark.asyncio
async def test_list_parses_legacy_metadata_without_optional_properties(
    session: Session, configured_token_encryption: Settings
) -> None:
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert metadata is not None
        return httpx.Response(200, json={"files": [_drive_file(metadata)]})

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id, include_optional=False)
    try:
        listed = await store.list(owner_id)
    finally:
        await store.aclose()

    assert listed[0].metadata.app_version is None
    assert listed[0].metadata.item_counts == {}


@pytest.mark.asyncio
async def test_list_rejects_unbounded_or_invalid_optional_metadata(
    session: Session, configured_token_encryption: Settings
) -> None:
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert metadata is not None
        payload = _drive_file(metadata)
        payload["appProperties"]["item_counts"] = '{"unknown":1}'
        return httpx.Response(200, json={"files": [payload]})

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        with pytest.raises(GoogleDriveMalformedPayloadError, match="item counts"):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_same_google_subject_on_second_installation_lists_and_authorizes_prior_snapshot(
    session: Session, configured_token_encryption: Settings
) -> None:
    # The first installation has a separate database; only its portable owner
    # identity remains in the remote snapshot metadata.
    first_workspace_owner_id = uuid4()
    second_workspace_owner_id, second_connection = _connection(
        session,
        configured_token_encryption,
        email="second-install@example.com",
        google_subject="shared-google-subject",
    )
    drive_owner_id = derive_drive_owner_id(second_connection.google_subject)
    metadata = BackupObjectMetadata(
        drive_owner_id=drive_owner_id,
        workspace_owner_id=first_workspace_owner_id,
        backup_id=uuid4(),
        schema_version=1,
        archive_checksum=CHECKSUM,
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        if request.method == "GET":
            assert str(drive_owner_id) in request.url.params["q"]
            return httpx.Response(200, json={"files": [_drive_file(metadata)]})
        if request.method == "DELETE":
            assert request.url.path.endswith(REMOTE_ID)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = GoogleDriveStore(
        connection=second_connection,
        oauth_service=GoogleDriveOAuthService(
            session=session, client=client, settings=configured_token_encryption
        ),
        client=client,
        settings=configured_token_encryption,
    )
    try:
        listed = await store.list(drive_owner_id)
        store.authorize_backup(listed[0])
        await store.delete(listed[0].remote_id)
    finally:
        await store.aclose()

    assert first_workspace_owner_id != second_workspace_owner_id
    assert listed[0].metadata.workspace_owner_id == first_workspace_owner_id


@pytest.mark.asyncio
async def test_different_google_subject_cannot_list_prior_snapshot(
    session: Session, configured_token_encryption: Settings
) -> None:
    prior_drive_owner_id = derive_drive_owner_id("prior-google-subject")
    _, connection = _connection(
        session,
        configured_token_encryption,
        email="other-install@example.com",
        google_subject="other-google-subject",
    )
    expected_drive_owner_id = derive_drive_owner_id(connection.google_subject)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert request.method == "GET"
        assert str(expected_drive_owner_id) in request.url.params["q"]
        assert str(prior_drive_owner_id) not in request.url.params["q"]
        return httpx.Response(200, json={"files": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = GoogleDriveStore(
        connection=connection,
        oauth_service=GoogleDriveOAuthService(
            session=session, client=client, settings=configured_token_encryption
        ),
        client=client,
        settings=configured_token_encryption,
    )
    try:
        assert await store.list(expected_drive_owner_id) == []
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_list_accepts_concrete_appdatafolder_parent_id(
    session: Session, configured_token_encryption: Settings
) -> None:
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert metadata is not None
        assert request.url.params["spaces"] == "appDataFolder"
        return httpx.Response(
            200,
            json={"files": [_drive_file(metadata, parents=["app_data_folder_id_12345"])]},
        )

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        backups = await store.list(owner_id)
        assert backups == [_stored_backup(metadata)]
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_list_rejects_missing_parent_id(
    session: Session, configured_token_encryption: Settings
) -> None:
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert metadata is not None
        return httpx.Response(200, json={"files": [_drive_file(metadata, parents=[])]})

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        with pytest.raises(GoogleDriveMalformedPayloadError, match="parent"):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_upload_rejects_completion_patch_for_a_different_remote_id(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    archive = tmp_path / "archive.zip"
    archive.write_bytes(ARCHIVE_BYTES)
    metadata: BackupObjectMetadata | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload.example/session"})
        assert metadata is not None
        if request.method == "PUT":
            return httpx.Response(200, json=_drive_file(metadata, completed=False))
        if request.method == "PATCH":
            return httpx.Response(
                200, json=_drive_file(metadata, completed=True, remote_id="other_file_67890")
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    store, owner_id = await _store(session, configured_token_encryption, handler)
    metadata = _metadata(owner_id)
    try:
        with pytest.raises(GoogleDriveMalformedPayloadError, match="identifier"):
            await store.upload(archive, metadata)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_delete_rejects_untrusted_or_malformed_remote_ids(
    session: Session, configured_token_encryption: Settings
) -> None:
    store, _ = await _store(session, configured_token_encryption, _token_response)
    try:
        with pytest.raises(InvalidGoogleDriveRemoteIdError):
            await store.delete("../not-an-id")
        with pytest.raises(InvalidGoogleDriveRemoteIdError):
            await store.delete(REMOTE_ID)
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_revoked_drive_credentials_require_reauthorization(
    session: Session, configured_token_encryption: Settings, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        return httpx.Response(status, json={"error": {"message": "do not expose"}})

    store, owner_id = await _store(session, configured_token_encryption, handler)
    try:
        with pytest.raises(GoogleDriveReauthorizationRequiredError):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_drive_responses_are_retryable(
    session: Session, configured_token_encryption: Settings, status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        return httpx.Response(status)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    try:
        with pytest.raises(GoogleDriveRetryableError):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_transient_token_refresh_is_retryable_for_store_operations(
    session: Session, configured_token_encryption: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == GoogleDriveOAuthService.TOKEN_URL
        return httpx.Response(429)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    try:
        with pytest.raises(GoogleDriveRetryableError):
            await store.list(owner_id)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_download_streams_to_atomic_destination_and_cleans_up_oversize_temp_file(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    destination = tmp_path / "restored.zip"
    destination.write_bytes(b"previous")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert request.url.params["alt"] == "media"
        return httpx.Response(200, content=b"x" * 65)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    store.authorize_backup(_stored_backup(_metadata(owner_id)))
    try:
        with pytest.raises(ValueError, match="maximum"):
            await store.download(REMOTE_ID, destination)
    finally:
        await store.aclose()

    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_download_atomically_replaces_destination_after_success(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    destination = tmp_path / "restored.zip"
    destination.write_bytes(b"previous")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        return httpx.Response(200, content=ARCHIVE_BYTES)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    store.authorize_backup(_stored_backup(_metadata(owner_id)))
    try:
        assert await store.download(REMOTE_ID, destination) == destination
    finally:
        await store.aclose()

    assert destination.read_bytes() == ARCHIVE_BYTES
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_download_maps_transport_errors_to_retryable_provider_errors(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        raise httpx.ConnectError("network unavailable", request=request)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    store.authorize_backup(_stored_backup(_metadata(owner_id)))
    try:
        with pytest.raises(GoogleDriveRetryableError):
            await store.download(REMOTE_ID, tmp_path / "restored.zip")
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_delete_accepts_only_locally_validated_backup_identity(
    session: Session, configured_token_encryption: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert request.method == "DELETE"
        return httpx.Response(204)

    store, owner_id = await _store(
        session, configured_token_encryption, handler, with_session=True
    )
    backup = _stored_backup(_metadata(owner_id))
    workspace_backup = WorkspaceBackup(
        user_id=store.connection.user_id,
        remote_file_id=REMOTE_ID,
        status=BackupStatus.completed,
        schema_version=1,
        archive_size_bytes=len(ARCHIVE_BYTES),
        checksum=CHECKSUM,
    )
    session.add(workspace_backup)
    session.commit()
    session.refresh(workspace_backup)
    assert workspace_backup.id is not None
    unpersisted = WorkspaceBackup(
        user_id=store.connection.user_id,
        remote_file_id=REMOTE_ID,
        status=BackupStatus.completed,
        schema_version=1,
        archive_size_bytes=len(ARCHIVE_BYTES),
        checksum=CHECKSUM,
    )
    detached = WorkspaceBackup(
        id=workspace_backup.id,
        user_id=store.connection.user_id,
        remote_file_id="detached_file_12345",
        status=BackupStatus.completed,
        schema_version=1,
        archive_size_bytes=len(ARCHIVE_BYTES),
        checksum=CHECKSUM,
    )
    try:
        with pytest.raises(TypeError):
            store.authorize_backup(REMOTE_ID)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="persisted"):
            store.authorize_backup(unpersisted)
        with pytest.raises(ValueError, match="persisted"):
            store.authorize_backup(detached)
        store.authorize_backup(backup)
        await store.delete(REMOTE_ID)
        store.authorize_backup(workspace_backup)
        await store.delete(REMOTE_ID)
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_delete_authorizes_trusted_incomplete_cognolith_backup(
    session: Session, configured_token_encryption: Settings
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        assert request.method == "DELETE"
        deleted.append(request.url.path.rsplit("/", maxsplit=1)[-1])
        return httpx.Response(204)

    store, owner_id = await _store(session, configured_token_encryption, handler)
    incomplete = replace(_stored_backup(_metadata(owner_id)), completed=False)
    try:
        store.authorize_backup(incomplete)
        await store.delete(incomplete.remote_id)
    finally:
        await store.aclose()

    assert deleted == [incomplete.remote_id]
