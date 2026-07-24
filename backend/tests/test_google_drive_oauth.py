from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError
from sqlmodel import Session, select

from app.core.config import Settings
from app.core.security import (
    ProviderTokenEncryptionError,
    decrypt_provider_token,
    encrypt_provider_token,
)
from app.models.backup import DriveConnectionStatus, GoogleDriveConnection, OAuthState
from app.models.user import User
from app.schemas.backup import (
    BackupOperationStatusResponse,
    GoogleDriveAuthorizationUrlResponse,
    GoogleDriveConnectionStatusResponse,
    RestoreConfirmationRequest,
    RestorePreviewResponse,
    WorkspaceBackupResponse,
)
from app.services.google_drive_oauth import (
    GoogleDriveOAuthError,
    GoogleDriveOAuthReauthorizationRequiredError,
    GoogleDriveOAuthRetryableError,
    GoogleDriveOAuthService,
    InvalidOAuthState,
    derive_drive_owner_id,
)

REQUIRED_SCOPE = "https://www.googleapis.com/auth/drive.appdata"


def test_drive_owner_id_is_stable_and_does_not_expose_google_subject() -> None:
    first = derive_drive_owner_id("google-subject-123")

    assert first == derive_drive_owner_id("google-subject-123")
    assert first != derive_drive_owner_id("other-google-subject")
    assert "google-subject-123" not in str(first)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": "test-secret",
        "SMTP_HOST": "mailpit",
        "SMTP_PORT": 1025,
        "SMTP_TLS": False,
        "EMAILS_FROM_EMAIL": "no-reply@example.com",
        "GOOGLE_DRIVE_CLIENT_ID": "google-client-id",
        "GOOGLE_DRIVE_CLIENT_SECRET": "google-client-secret",
        "GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def provider_token_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    configured_settings = _settings()
    monkeypatch.setattr("app.core.security.settings", configured_settings)
    return configured_settings


class PersistedUser(Protocol):
    id: int
    email: str


def _create_user(session: Session, email: str = "drive@example.com") -> PersistedUser:
    user = User(email=email, hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    return cast(PersistedUser, user)


def _oauth_service(session: Session, client: Any, settings: Settings) -> GoogleDriveOAuthService:
    return GoogleDriveOAuthService(session=session, client=client, settings=settings)


def _encrypt_with_settings(value: str, settings: Settings) -> bytes:
    assert settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY is not None
    return Fernet(settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY.encode()).encrypt(value.encode())


def _google_response(request: httpx.Request) -> httpx.Response:
    if request.url == GoogleDriveOAuthService.TOKEN_URL:
        return httpx.Response(
            200,
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "scope": REQUIRED_SCOPE,
                "token_type": "Bearer",
            },
        )
    if request.url == GoogleDriveOAuthService.USERINFO_URL:
        assert request.headers["Authorization"] == "Bearer access-secret"
        return httpx.Response(200, json={"sub": "google-subject", "email": "drive@example.com"})
    if request.url == GoogleDriveOAuthService.REVOCATION_URL:
        return httpx.Response(200)
    raise AssertionError(f"Unexpected request to {request.url}")


def test_provider_token_round_trip_does_not_store_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    monkeypatch.setattr("app.core.security.settings", settings)

    encrypted = encrypt_provider_token("refresh-secret")

    assert b"refresh-secret" not in encrypted
    assert decrypt_provider_token(encrypted) == "refresh-secret"


def test_provider_token_rejects_missing_or_invalid_encryption_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.security.settings", SimpleNamespace(GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY=None)
    )

    with pytest.raises(ProviderTokenEncryptionError, match="not configured"):
        encrypt_provider_token("refresh-secret")

    monkeypatch.setattr(
        "app.core.security.settings",
        SimpleNamespace(GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY="not-a-fernet-key"),
    )

    with pytest.raises(ProviderTokenEncryptionError, match="invalid"):
        decrypt_provider_token(b"not-a-token")


def test_google_drive_backup_is_disabled_without_complete_configuration() -> None:
    settings = _settings(GOOGLE_DRIVE_CLIENT_SECRET=None)

    assert settings.google_drive_backup_enabled is False


def test_production_rejects_partial_google_drive_configuration() -> None:
    with pytest.raises(ValidationError, match="Google Drive backup configuration is incomplete"):
        _settings(
            ENVIRONMENT="production",
            GOOGLE_DRIVE_CLIENT_SECRET=None,
            POSTGRES_PASSWORD="a-secure-database-password",
            FIRST_SUPERUSER_PASSWORD="a-secure-administrator-password",
            DATABASE_SSL_MODE="require",
        )


def test_oauth_state_is_single_use(session: Session) -> None:
    service = GoogleDriveOAuthService(session=session, settings=_settings())
    user = _create_user(session)

    state = service.create_state(user_id=user.id)
    service.consume_state(user_id=user.id, state=state)

    with pytest.raises(InvalidOAuthState):
        service.consume_state(user_id=user.id, state=state)


def test_oauth_state_rejects_expired_and_wrong_user_values(session: Session) -> None:
    service = GoogleDriveOAuthService(session=session, settings=_settings())
    user = _create_user(session)
    state = service.create_state(user_id=user.id)

    with pytest.raises(InvalidOAuthState):
        service.consume_state(user_id=user.id + 1, state=state)

    state_record = session.exec(select(OAuthState).where(OAuthState.user_id == user.id)).one()
    state_record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(state_record)
    session.commit()

    with pytest.raises(InvalidOAuthState):
        service.consume_state(user_id=user.id, state=state)


def test_authorization_url_requests_app_data_identity_scopes_and_offline_consent(
    session: Session,
) -> None:
    service = GoogleDriveOAuthService(session=session, settings=_settings())
    user = _create_user(session)

    authorization_url = service.create_authorization_url(user_id=user.id)
    params = parse_qs(urlparse(authorization_url).query)

    assert authorization_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["client_id"] == ["google-client-id"]
    assert params["redirect_uri"] == [_settings().GOOGLE_DRIVE_REDIRECT_URI]
    assert params["response_type"] == ["code"]
    assert set(params["scope"][0].split()) == {REQUIRED_SCOPE, "openid", "email"}
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert len(params["state"][0]) >= 32


@pytest.mark.asyncio
async def test_complete_authorization_exchanges_code_and_upserts_connection(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_google_response)) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)

        connection = await service.complete_authorization(user_id=user.id, state=state, code="code-secret")

    assert connection.user_id == user.id
    assert connection.google_subject == "google-subject"
    assert connection.google_email == "drive@example.com"
    assert connection.granted_scopes == [REQUIRED_SCOPE]
    assert connection.status == DriveConnectionStatus.connected
    assert connection.token_expires_at is not None
    assert b"refresh-secret" not in connection.encrypted_refresh_token
    assert (
        decrypt_provider_token(
            connection.encrypted_refresh_token,
            encryption_key=settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY,
        )
        == "refresh-secret"
    )


@pytest.mark.asyncio
async def test_google_subject_cannot_be_connected_to_two_local_users(
    session: Session,
) -> None:
    first_user = _create_user(session, email="first-drive-owner@example.com")
    second_user = _create_user(session, email="second-drive-owner@example.com")
    settings = _settings()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_google_response)
    ) as client:
        service = _oauth_service(session, client, settings)
        first_state = service.create_state(user_id=first_user.id)
        await service.complete_authorization(
            user_id=first_user.id,
            state=first_state,
            code="first-code",
        )
        second_state = service.create_state(user_id=second_user.id)
        with pytest.raises(GoogleDriveOAuthError, match="already connected"):
            await service.complete_authorization(
                user_id=second_user.id,
                state=second_state,
                code="second-code",
            )

    connections = session.exec(select(GoogleDriveConnection)).all()
    assert len(connections) == 1
    assert connections[0].user_id == first_user.id


@pytest.mark.asyncio
async def test_complete_authorization_preserves_existing_refresh_token(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    encrypted_refresh_token = _encrypt_with_settings("existing-refresh-secret", settings)
    existing = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=encrypted_refresh_token,
        google_subject="old-subject",
        google_email="old@example.com",
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(existing)
    session.commit()

    def response_without_refresh_token(request: httpx.Request) -> httpx.Response:
        response = _google_response(request)
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            payload = response.json()
            payload.pop("refresh_token")
            return httpx.Response(200, json=payload)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response_without_refresh_token)
    ) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)
        connection = await service.complete_authorization(user_id=user.id, state=state, code="code-secret")

    assert connection.id == existing.id
    assert (
        decrypt_provider_token(
            connection.encrypted_refresh_token,
            encryption_key=settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY,
        )
        == "existing-refresh-secret"
    )


@pytest.mark.asyncio
async def test_complete_authorization_rejects_empty_disconnected_connection_without_refresh_token(
    session: Session,
) -> None:
    user = _create_user(session)
    settings = _settings()
    existing = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=b"",
        google_subject="old-subject",
        google_email="old@example.com",
        granted_scopes=[REQUIRED_SCOPE],
        status=DriveConnectionStatus.disconnected,
    )
    session.add(existing)
    session.commit()

    def response_without_refresh_token(request: httpx.Request) -> httpx.Response:
        response = _google_response(request)
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            payload = response.json()
            payload.pop("refresh_token")
            return httpx.Response(200, json=payload)
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response_without_refresh_token)
    ) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)

        with pytest.raises(GoogleDriveOAuthError, match="did not return a refresh token"):
            await service.complete_authorization(user_id=user.id, state=state, code="code-secret")

    session.refresh(existing)
    assert existing.status == DriveConnectionStatus.disconnected
    assert existing.encrypted_refresh_token == b""


@pytest.mark.asyncio
async def test_complete_authorization_encrypts_with_service_token_encryption_key(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _create_user(session)
    settings = _settings()
    monkeypatch.setattr(
        "app.core.security.settings",
        SimpleNamespace(
            GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY=None,
            SECRET_KEY=Fernet.generate_key().decode(),
        ),
    )
    ciphertext: bytes | None = None

    async with httpx.AsyncClient(transport=httpx.MockTransport(_google_response)) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)
        try:
            connection = await service.complete_authorization(
                user_id=user.id, state=state, code="code-secret"
            )
        except ProviderTokenEncryptionError:
            pass
        else:
            ciphertext = connection.encrypted_refresh_token

    plaintext: bytes | None = None
    if ciphertext is not None:
        assert settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY is not None
        try:
            plaintext = Fernet(settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY.encode()).decrypt(ciphertext)
        except InvalidToken:
            pass
    assert plaintext == b"refresh-secret"


@pytest.mark.asyncio
async def test_refresh_access_token_decrypts_with_service_token_encryption_key(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()
    monkeypatch.setattr(
        "app.core.security.settings",
        SimpleNamespace(
            GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY=None,
            SECRET_KEY=Fernet.generate_key().decode(),
        ),
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_google_response)) as client:
        try:
            access_token = await _oauth_service(session, client, settings).refresh_access_token(connection)
        except ProviderTokenEncryptionError:
            access_token = None

    assert access_token == "access-secret"


@pytest.mark.asyncio
async def test_complete_authorization_rejects_missing_required_scope(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()

    def insufficient_scope_response(request: httpx.Request) -> httpx.Response:
        if request.url == GoogleDriveOAuthService.TOKEN_URL:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/drive.file",
                },
            )
        raise AssertionError(f"Unexpected request to {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(insufficient_scope_response)
    ) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)

        with pytest.raises(GoogleDriveOAuthError, match="required permissions"):
            await service.complete_authorization(user_id=user.id, state=state, code="code-secret")


@pytest.mark.asyncio
async def test_refresh_access_token_uses_google_token_endpoint(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()

    def refresh_response(request: httpx.Request) -> httpx.Response:
        assert request.url == GoogleDriveOAuthService.TOKEN_URL
        assert request.method == "POST"
        return httpx.Response(200, json={"access_token": "new-access-secret", "expires_in": 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refresh_response)) as client:
        access_token = await _oauth_service(session, client, settings).refresh_access_token(connection)

    assert access_token == "new-access-secret"
    assert connection.token_expires_at is not None


@pytest.mark.asyncio
async def test_refresh_access_token_persists_rotated_refresh_token(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("old-refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()

    def refresh_response(request: httpx.Request) -> httpx.Response:
        assert request.url == GoogleDriveOAuthService.TOKEN_URL
        return httpx.Response(
            200,
            json={
                "access_token": "new-access-secret",
                "refresh_token": "rotated-refresh-secret",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(refresh_response)) as client:
        await _oauth_service(session, client, settings).refresh_access_token(connection)

    assert b"rotated-refresh-secret" not in connection.encrypted_refresh_token
    assert (
        decrypt_provider_token(
            connection.encrypted_refresh_token,
            encryption_key=settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY,
        )
        == "rotated-refresh-secret"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_refresh_access_token_maps_transient_responses_to_retryable_error(
    session: Session, status: int
) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        with pytest.raises(GoogleDriveOAuthRetryableError):
            await _oauth_service(session, client, settings).refresh_access_token(connection)


@pytest.mark.asyncio
async def test_refresh_access_token_maps_transport_error_to_retryable_error(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )

    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport_error)) as client:
        with pytest.raises(GoogleDriveOAuthRetryableError):
            await _oauth_service(session, client, settings).refresh_access_token(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload"),
    [(401, {}), (403, {}), (400, {"error": "invalid_grant"})],
)
async def test_refresh_access_token_maps_revocation_to_reauthorization_error(
    session: Session, status: int, payload: dict[str, str]
) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    ) as client:
        with pytest.raises(GoogleDriveOAuthReauthorizationRequiredError):
            await _oauth_service(session, client, settings).refresh_access_token(connection)


@pytest.mark.asyncio
async def test_disconnect_attempts_revocation_and_disables_local_connection(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(_google_response)) as client:
        await _oauth_service(session, client, settings).disconnect(connection)

    assert connection.status == DriveConnectionStatus.disconnected
    assert connection.disconnected_at is not None
    assert connection.encrypted_refresh_token == b""
    assert connection.token_expires_at is None


@pytest.mark.asyncio
async def test_disconnect_disables_local_connection_when_revocation_fails(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=_encrypt_with_settings("refresh-secret", settings),
        google_subject="google-subject",
        google_email=user.email,
        granted_scopes=[REQUIRED_SCOPE],
    )
    session.add(connection)
    session.commit()

    async def failing_post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("network unavailable")

    client = SimpleNamespace(post=failing_post)
    await _oauth_service(session, client, settings).disconnect(connection)

    assert connection.status == DriveConnectionStatus.disconnected
    assert connection.encrypted_refresh_token == b""


@pytest.mark.asyncio
async def test_oauth_errors_do_not_expose_sensitive_provider_values(session: Session) -> None:
    user = _create_user(session)
    settings = _settings()

    def rejected_token_response(request: httpx.Request) -> httpx.Response:
        assert request.url == GoogleDriveOAuthService.TOKEN_URL
        return httpx.Response(400, json={"error": "invalid_grant", "detail": "code-secret access-secret"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(rejected_token_response)
    ) as client:
        service = _oauth_service(session, client, settings)
        state = service.create_state(user_id=user.id)

        with pytest.raises(GoogleDriveOAuthError) as error:
            await service.complete_authorization(user_id=user.id, state=state, code="code-secret")

    assert "code-secret" not in str(error.value)
    assert "access-secret" not in str(error.value)


def test_connection_status_schema_does_not_expose_encrypted_credentials() -> None:
    schemas = [
        BackupOperationStatusResponse,
        GoogleDriveAuthorizationUrlResponse,
        GoogleDriveConnectionStatusResponse,
        RestoreConfirmationRequest,
        RestorePreviewResponse,
        WorkspaceBackupResponse,
    ]

    assert all("encrypted_refresh_token" not in schema.model_fields for schema in schemas)
