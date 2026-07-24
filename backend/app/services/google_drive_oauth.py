import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlmodel import Session, select

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.core.security import decrypt_provider_token, encrypt_provider_token
from app.models.backup import DriveConnectionStatus, GoogleDriveConnection, OAuthState


class InvalidOAuthState(ValueError):  # noqa: N818
    """Raised when an OAuth callback state is absent, expired, or already used."""


class GoogleDriveOAuthError(RuntimeError):
    """Raised for OAuth failures without provider payload details."""


class GoogleDriveOAuthService:
    AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
    REVOCATION_URL = "https://oauth2.googleapis.com/revoke"
    REQUIRED_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
    IDENTITY_SCOPES = ("openid", "email")
    STATE_TTL = timedelta(minutes=10)

    def __init__(
        self,
        session: Session,
        client: httpx.AsyncClient | Any | None = None,
        settings: Settings = default_settings,
    ) -> None:
        self.session = session
        self.client = client
        self.settings = settings

    def create_state(self, user_id: int) -> str:
        state = secrets.token_urlsafe(32)
        state_record = OAuthState(
            user_id=user_id,
            state_hash=self._hash_state(state),
            expires_at=datetime.now(UTC) + self.STATE_TTL,
        )
        self.session.add(state_record)
        self.session.commit()
        return state

    def consume_state(self, user_id: int, state: str) -> None:
        state_hash = self._hash_state(state)
        candidates = self.session.exec(
            select(OAuthState).where(OAuthState.user_id == user_id).with_for_update()
        ).all()
        state_record = next(
            (
                candidate
                for candidate in candidates
                if hmac.compare_digest(candidate.state_hash, state_hash)
            ),
            None,
        )
        if (
            state_record is None
            or state_record.consumed_at is not None
            or state_record.expires_at <= datetime.now(UTC)
        ):
            raise InvalidOAuthState("Google Drive authorization state is invalid or expired")

        state_record.consumed_at = datetime.now(UTC)
        self.session.add(state_record)
        self.session.commit()

    def create_authorization_url(self, user_id: int) -> str:
        self._require_oauth_configuration()
        parameters = {
            "client_id": self.settings.GOOGLE_DRIVE_CLIENT_ID,
            "redirect_uri": self.settings.GOOGLE_DRIVE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join((self.REQUIRED_SCOPE, *self.IDENTITY_SCOPES)),
            "state": self.create_state(user_id),
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{self.AUTHORIZATION_URL}?{urlencode(parameters)}"

    async def complete_authorization(
        self, user_id: int, state: str, code: str
    ) -> GoogleDriveConnection:
        self._require_oauth_configuration()
        self.consume_state(user_id=user_id, state=state)
        token_payload = await self._post_json(
            self.TOKEN_URL,
            data={
                "client_id": self.settings.GOOGLE_DRIVE_CLIENT_ID,
                "client_secret": self.settings.GOOGLE_DRIVE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.GOOGLE_DRIVE_REDIRECT_URI,
            },
            failure_message="Google Drive authorization could not be completed",
        )
        access_token = self._required_string(
            token_payload, "access_token", "Google Drive authorization could not be completed"
        )
        scopes = self._parse_scopes(token_payload)
        if self.REQUIRED_SCOPE not in scopes:
            raise GoogleDriveOAuthError("Google Drive authorization did not grant required permissions")

        identity_payload = await self._get_json(
            self.USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            failure_message="Google Drive identity could not be verified",
        )
        google_subject = self._required_string(
            identity_payload, "sub", "Google Drive identity could not be verified"
        )
        google_email = self._required_string(
            identity_payload, "email", "Google Drive identity could not be verified"
        )
        connection = self.session.exec(
            select(GoogleDriveConnection)
            .where(GoogleDriveConnection.user_id == user_id)
            .with_for_update()
        ).first()
        refresh_token = token_payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token:
            encrypted_refresh_token = encrypt_provider_token(refresh_token)
        elif connection is not None:
            encrypted_refresh_token = connection.encrypted_refresh_token
        else:
            raise GoogleDriveOAuthError("Google Drive authorization did not return a refresh token")

        expires_in = self._expires_at(token_payload)
        if connection is None:
            connection = GoogleDriveConnection(
                user_id=user_id,
                encrypted_refresh_token=encrypted_refresh_token,
                google_subject=google_subject,
                google_email=google_email,
                granted_scopes=scopes,
                token_expires_at=expires_in,
            )
        else:
            connection.encrypted_refresh_token = encrypted_refresh_token
            connection.google_subject = google_subject
            connection.google_email = google_email
            connection.granted_scopes = scopes
            connection.token_expires_at = expires_in
            connection.status = DriveConnectionStatus.connected
            connection.disconnected_at = None
        self.session.add(connection)
        self.session.commit()
        self.session.refresh(connection)
        return connection

    async def refresh_access_token(self, connection: GoogleDriveConnection) -> str:
        self._require_oauth_configuration()
        if connection.status != DriveConnectionStatus.connected or not connection.encrypted_refresh_token:
            raise GoogleDriveOAuthError("Google Drive connection is not active")
        refresh_token = decrypt_provider_token(connection.encrypted_refresh_token)
        token_payload = await self._post_json(
            self.TOKEN_URL,
            data={
                "client_id": self.settings.GOOGLE_DRIVE_CLIENT_ID,
                "client_secret": self.settings.GOOGLE_DRIVE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            failure_message="Google Drive access token could not be refreshed",
        )
        access_token = self._required_string(
            token_payload, "access_token", "Google Drive access token could not be refreshed"
        )
        connection.token_expires_at = self._expires_at(token_payload)
        self.session.add(connection)
        self.session.commit()
        return access_token

    async def disconnect(self, connection: GoogleDriveConnection) -> None:
        try:
            refresh_token = decrypt_provider_token(connection.encrypted_refresh_token)
            await self._post(self.REVOCATION_URL, data={"token": refresh_token})
        except Exception:
            # Google revocation is best effort; credentials are still disabled locally below.
            pass

        connection.encrypted_refresh_token = b""
        connection.token_expires_at = None
        connection.status = DriveConnectionStatus.disconnected
        connection.disconnected_at = datetime.now(UTC)
        self.session.add(connection)
        self.session.commit()

    @staticmethod
    def _hash_state(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    def _require_oauth_configuration(self) -> None:
        if not self.settings.google_drive_backup_enabled:
            raise GoogleDriveOAuthError("Google Drive backup is not configured")

    async def _post_json(
        self, url: str, data: dict[str, str | None], failure_message: str
    ) -> dict[str, Any]:
        response = await self._post(url, data=data)
        if response.is_error:
            raise GoogleDriveOAuthError(failure_message)
        return self._json_payload(response, failure_message)

    async def _get_json(
        self, url: str, headers: dict[str, str], failure_message: str
    ) -> dict[str, Any]:
        if self.client is not None:
            response = await self.client.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
        if response.is_error:
            raise GoogleDriveOAuthError(failure_message)
        return self._json_payload(response, failure_message)

    async def _post(self, url: str, data: dict[str, str | None]) -> httpx.Response:
        if self.client is not None:
            return await self.client.post(url, data=data)
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(url, data=data)

    @staticmethod
    def _json_payload(response: httpx.Response, failure_message: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise GoogleDriveOAuthError(failure_message) from error
        if not isinstance(payload, dict):
            raise GoogleDriveOAuthError(failure_message)
        return payload

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str, failure_message: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise GoogleDriveOAuthError(failure_message)
        return value

    def _parse_scopes(self, payload: dict[str, Any]) -> list[str]:
        raw_scopes = payload.get("scope")
        if isinstance(raw_scopes, str):
            return raw_scopes.split()
        if isinstance(raw_scopes, list) and all(isinstance(scope, str) for scope in raw_scopes):
            return raw_scopes
        return []

    @staticmethod
    def _expires_at(payload: dict[str, Any]) -> datetime | None:
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, int) or expires_in <= 0:
            return None
        return datetime.now(UTC) + timedelta(seconds=expires_in)
