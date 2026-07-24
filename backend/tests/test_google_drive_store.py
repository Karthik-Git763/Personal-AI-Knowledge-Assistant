import json
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
from app.models.backup import GoogleDriveConnection
from app.models.user import User
from app.services.backup_store import BackupObjectMetadata
from app.services.google_drive_oauth import GoogleDriveOAuthService
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


def _connection(session: Session, settings: Settings) -> tuple[UUID, GoogleDriveConnection]:
    user = User(email="drive-store@example.com", hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=encrypt_provider_token(
            "refresh-secret", encryption_key=settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY
        ),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[GoogleDriveOAuthService.REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()
    return user.portable_id, connection


def _metadata(owner_id: UUID) -> BackupObjectMetadata:
    return BackupObjectMetadata(
        owner_id=owner_id,
        backup_id=uuid4(),
        schema_version=1,
        archive_checksum=CHECKSUM,
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )


def _drive_file(metadata: BackupObjectMetadata, *, completed: bool = True) -> dict[str, Any]:
    return {
        "id": REMOTE_ID,
        "name": f"cognolith-{metadata.backup_id}.zip",
        "size": str(len(ARCHIVE_BYTES)),
        "createdTime": metadata.created_at.isoformat().replace("+00:00", "Z"),
        "appProperties": {
            "cognolith": "backup",
            "owner_id": str(metadata.owner_id),
            "backup_id": str(metadata.backup_id),
            "schema_version": str(metadata.schema_version),
            "archive_checksum": metadata.archive_checksum,
            "created_at": metadata.created_at.isoformat().replace("+00:00", "Z"),
            "completed": str(completed).lower(),
        },
    }


async def _store(
    session: Session, settings: Settings, handler: httpx.MockTransport | Any
) -> tuple[GoogleDriveStore, UUID]:
    owner_id, connection = _connection(session, settings)
    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    oauth_service = GoogleDriveOAuthService(session=session, client=client, settings=settings)
    return (
        GoogleDriveStore(
            owner_id=owner_id,
            connection=connection,
            oauth_service=oauth_service,
            client=client,
            settings=settings,
        ),
        owner_id,
    )


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
            assert body["parents"] == ["appDataFolder"]
            assert body["appProperties"]["completed"] == "false"
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
        assert "owner_id" in query
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

    store, _ = await _store(session, configured_token_encryption, handler)
    store.authorize_remote_id(REMOTE_ID)
    try:
        with pytest.raises(ValueError, match="maximum"):
            await store.download(REMOTE_ID, destination)
    finally:
        await store.aclose()

    assert destination.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_download_maps_transport_errors_to_retryable_provider_errors(
    session: Session, tmp_path: Path, configured_token_encryption: Settings
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return _token_response(request)
        raise httpx.ConnectError("network unavailable", request=request)

    store, _ = await _store(session, configured_token_encryption, handler)
    store.authorize_remote_id(REMOTE_ID)
    try:
        with pytest.raises(GoogleDriveRetryableError):
            await store.download(REMOTE_ID, tmp_path / "restored.zip")
    finally:
        await store.aclose()
