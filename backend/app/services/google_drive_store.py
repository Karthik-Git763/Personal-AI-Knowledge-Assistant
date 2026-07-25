from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlmodel import Session, select

from app.core.config import Settings
from app.core.config import settings as default_settings
from app.models.backup import BackupStatus, GoogleDriveConnection, WorkspaceBackup
from app.services.backup_store import (
    BackupObjectMetadata,
    StoredBackup,
    is_trusted_stored_backup,
)
from app.services.google_drive_oauth import (
    GoogleDriveOAuthError,
    GoogleDriveOAuthReauthorizationRequiredError,
    GoogleDriveOAuthRetryableError,
    GoogleDriveOAuthService,
    derive_drive_owner_id,
)


class GoogleDriveStoreError(RuntimeError):
    """Base error for Google Drive backup storage operations."""


class GoogleDriveMalformedPayloadError(GoogleDriveStoreError):
    """Google Drive returned a payload that cannot represent a Cognolith backup."""


class GoogleDriveReauthorizationRequiredError(GoogleDriveStoreError):
    """The connected Drive credentials are no longer authorized."""


class GoogleDriveRetryableError(GoogleDriveStoreError):
    """Google Drive or the network returned a transient failure."""


class InvalidGoogleDriveRemoteIdError(GoogleDriveStoreError):
    """A Drive object identifier is malformed or was not locally authorized."""


class GoogleDriveStore:
    DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
    UPLOAD_API_URL = "https://www.googleapis.com/upload/drive/v3"
    _CHUNK_SIZE = 64 * 1024
    _REMOTE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
    _CHECKSUM = re.compile(r"[0-9a-f]{64}")
    _APP_VERSION = re.compile(r"[\x21-\x7e]{1,64}")
    _COUNT_ALIASES = {
        "n": "notes",
        "f": "folders",
        "t": "tags",
        "r": "note_tag_relations",
        "l": "links",
        "p": "templates",
        "d": "documents",
        "s": "chat_sessions",
        "m": "chat_messages",
        "u": "user_preferences",
    }
    _COUNT_KEYS = {value: key for key, value in _COUNT_ALIASES.items()}
    _MAX_ITEM_COUNTS_PROPERTY_LENGTH = 124
    _FIELDS = "id,name,size,createdTime,parents,appProperties"

    def __init__(
        self,
        connection: GoogleDriveConnection,
        oauth_service: GoogleDriveOAuthService,
        client: httpx.AsyncClient | None = None,
        settings: Settings = default_settings,
        session: Session | None = None,
    ) -> None:
        self.connection = connection
        self.drive_owner_id = derive_drive_owner_id(connection.google_subject)
        self.oauth_service = oauth_service
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.settings = settings
        self.session = session
        self._trusted_remote_ids: set[str] = set()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def upload(self, archive: Path, metadata: BackupObjectMetadata) -> StoredBackup:
        self._validate_metadata(metadata)
        if metadata.drive_owner_id != self.drive_owner_id:
            raise ValueError("Backup metadata belongs to another Drive identity")
        if not archive.is_file():
            raise ValueError("Backup archive must be a regular file")
        size = archive.stat().st_size
        if size > self.settings.BACKUP_MAX_ARCHIVE_SIZE:
            raise ValueError("Backup archive exceeds the configured maximum size")

        app_properties = self._app_properties(metadata, completed=False)
        authorization_headers = await self._authorization_headers()
        init_response = await self._request(
            "POST",
            f"{self.UPLOAD_API_URL}/files",
            params={"uploadType": "resumable", "fields": self._FIELDS},
            json={
                "name": f"cognolith-{metadata.backup_id}.zip",
                "parents": ["appDataFolder"],
                "appProperties": app_properties,
            },
            extra_headers={
                "X-Upload-Content-Type": "application/zip",
                "X-Upload-Content-Length": str(size),
            },
            authorization_headers=authorization_headers,
        )
        location = init_response.headers.get("Location")
        if not self._is_resumable_location(location):
            raise GoogleDriveMalformedPayloadError("Google Drive did not return an upload location")

        upload_response = await self._request(
            "PUT",
            location,
            content=self._stream_file(archive),
            extra_headers={"Content-Type": "application/zip", "Content-Length": str(size)},
            authorization_headers=authorization_headers,
        )
        uploaded = self._parse_backup(
            self._json_object(upload_response), metadata=metadata, completed=False, size=size
        )
        completed_properties = self._app_properties(metadata, completed=True)
        completion_response = await self._request(
            "PATCH",
            f"{self.DRIVE_API_URL}/files/{uploaded.remote_id}",
            params={"fields": self._FIELDS},
            json={"appProperties": completed_properties},
            authorization_headers=authorization_headers,
        )
        completed = self._parse_backup(
            self._json_object(completion_response), metadata=metadata, completed=True, size=size
        )
        if completed.remote_id != uploaded.remote_id:
            raise GoogleDriveMalformedPayloadError("Google Drive completion changed the file identifier")
        self._trusted_remote_ids.add(completed.remote_id)
        return completed

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]:
        if drive_owner_id != self.drive_owner_id:
            raise ValueError("Google Drive store is bound to a different Drive identity")
        query = self._list_query(drive_owner_id)
        files: list[StoredBackup] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {
                "spaces": "appDataFolder",
                "q": query,
                "fields": f"files({self._FIELDS}),nextPageToken",
                "pageSize": 1000,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            response = await self._request("GET", f"{self.DRIVE_API_URL}/files", params=params)
            payload = self._json_object(response)
            raw_files = payload.get("files")
            if not isinstance(raw_files, list):
                raise GoogleDriveMalformedPayloadError("Google Drive response is missing files")
            for raw_file in raw_files:
                backup = self._parse_backup(raw_file)
                if backup.metadata.drive_owner_id != drive_owner_id:
                    raise GoogleDriveMalformedPayloadError("Google Drive returned another Drive owner's backup")
                files.append(backup)
                self._trusted_remote_ids.add(backup.remote_id)
            raw_page_token = payload.get("nextPageToken")
            if raw_page_token is None:
                return files
            if not isinstance(raw_page_token, str) or not raw_page_token:
                raise GoogleDriveMalformedPayloadError("Google Drive returned an invalid page token")
            page_token = raw_page_token

    async def download(self, remote_id: str, destination: Path) -> Path:
        self._require_trusted_remote_id(remote_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_handle = tempfile.NamedTemporaryFile(
            mode="wb", delete=False, dir=destination.parent, prefix=f".{destination.name}.", suffix=".part"
        )
        temporary = Path(temporary_handle.name)
        temporary_handle.close()
        try:
            authorization_headers = await self._authorization_headers()
            async with self.client.stream(
                "GET",
                f"{self.DRIVE_API_URL}/files/{remote_id}",
                params={"alt": "media"},
                headers=authorization_headers,
            ) as response:
                self._check_response(response)
                size = 0
                with temporary.open("wb") as temp_file:
                    async for chunk in response.aiter_bytes(self._CHUNK_SIZE):
                        size += len(chunk)
                        if size > self.settings.BACKUP_MAX_ARCHIVE_SIZE:
                            raise ValueError("Downloaded backup exceeds the configured maximum size")
                        temp_file.write(chunk)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
            os.replace(temporary, destination)
        except httpx.RequestError as error:
            self._unlink_after_failure(temporary)
            raise GoogleDriveRetryableError("Google Drive request could not be completed") from error
        except BaseException:
            self._unlink_after_failure(temporary)
            raise
        temporary.unlink(missing_ok=True)
        return destination

    async def delete(self, remote_id: str) -> None:
        self._require_trusted_remote_id(remote_id)
        await self._request("DELETE", f"{self.DRIVE_API_URL}/files/{remote_id}")
        self._trusted_remote_ids.discard(remote_id)

    def authorize_backup(self, backup: StoredBackup | WorkspaceBackup) -> None:
        """Allow an ID from a validated stored backup or completed local backup record."""
        if isinstance(backup, StoredBackup):
            if not is_trusted_stored_backup(backup, self.drive_owner_id):
                raise ValueError("Stored backup is not valid for this owner")
            remote_id = backup.remote_id
        elif isinstance(backup, WorkspaceBackup):
            remote_id = self._validated_workspace_backup_remote_id(backup)
        else:
            raise TypeError("Backup authorization requires a stored or workspace backup")
        self._validate_remote_id(remote_id)
        self._trusted_remote_ids.add(remote_id)

    def _validated_workspace_backup_remote_id(self, backup: WorkspaceBackup) -> str:
        if self.session is None or backup.id is None or not backup.remote_file_id:
            raise ValueError("Workspace backup must be a persisted validated record")
        record = self.session.exec(
            select(WorkspaceBackup).where(
                WorkspaceBackup.id == backup.id,
                WorkspaceBackup.user_id == self.connection.user_id,
                WorkspaceBackup.remote_file_id == backup.remote_file_id,
                WorkspaceBackup.status == BackupStatus.completed,
            )
        ).one_or_none()
        if (
            record is None
            or record.schema_version < 1
            or record.archive_size_bytes is None
            or record.archive_size_bytes < 0
            or not record.checksum
            or not self._CHECKSUM.fullmatch(record.checksum)
            or not record.remote_file_id
        ):
            raise ValueError("Workspace backup must be a persisted validated record")
        return record.remote_file_id

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("extra_headers", {}))
        authorization_headers = kwargs.pop("authorization_headers", None)
        if authorization_headers is None:
            authorization_headers = await self._authorization_headers()
        headers.update(authorization_headers)
        try:
            response = await self.client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as error:
            raise GoogleDriveRetryableError("Google Drive request could not be completed") from error
        self._check_response(response)
        return response

    async def _authorization_headers(self) -> dict[str, str]:
        try:
            token = await self.oauth_service.refresh_access_token(self.connection)
        except GoogleDriveOAuthRetryableError as error:
            raise GoogleDriveRetryableError("Google Drive authorization is temporarily unavailable") from error
        except GoogleDriveOAuthReauthorizationRequiredError as error:
            raise GoogleDriveReauthorizationRequiredError("Google Drive authorization is required") from error
        except GoogleDriveOAuthError as error:
            raise GoogleDriveReauthorizationRequiredError("Google Drive authorization is required") from error
        return {"Authorization": f"Bearer {token}"}

    def _check_response(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise GoogleDriveReauthorizationRequiredError("Google Drive authorization is required")
        if response.status_code == 429 or response.status_code >= 500:
            raise GoogleDriveRetryableError("Google Drive is temporarily unavailable")
        if response.is_error:
            raise GoogleDriveStoreError("Google Drive request failed")

    def _parse_backup(
        self,
        raw_file: object,
        *,
        metadata: BackupObjectMetadata | None = None,
        completed: bool | None = None,
        size: int | None = None,
    ) -> StoredBackup:
        if not isinstance(raw_file, Mapping):
            raise GoogleDriveMalformedPayloadError("Google Drive file payload is invalid")
        remote_id = self._required_string(raw_file, "id")
        self._validate_remote_id(remote_id)
        name = self._required_string(raw_file, "name")
        raw_size = self._required_string(raw_file, "size")
        if not raw_size.isdecimal():
            raise GoogleDriveMalformedPayloadError("Google Drive file size is invalid")
        parsed_size = int(raw_size)
        if parsed_size > self.settings.BACKUP_MAX_ARCHIVE_SIZE:
            raise GoogleDriveMalformedPayloadError(
                "Google Drive backup exceeds the configured maximum size"
            )
        properties = raw_file.get("appProperties")
        if not isinstance(properties, Mapping):
            raise GoogleDriveMalformedPayloadError("Google Drive file properties are invalid")
        parsed_metadata = self._metadata_from_properties(properties)
        parsed_completed = self._completed_property(properties)
        self._parse_datetime(self._required_string(raw_file, "createdTime"))
        parents = raw_file.get("parents")
        if (
            not isinstance(parents, list)
            or len(parents) != 1
            or not isinstance(parents[0], str)
            or not parents[0]
        ):
            raise GoogleDriveMalformedPayloadError("Google Drive file parent is invalid")
        backup = StoredBackup(
            remote_id=remote_id,
            name=name,
            size=parsed_size,
            created_at=parsed_metadata.created_at,
            metadata=parsed_metadata,
            completed=parsed_completed,
        )
        if metadata is not None and backup.metadata != metadata:
            raise GoogleDriveMalformedPayloadError("Google Drive upload metadata did not validate")
        if completed is not None and backup.completed is not completed:
            raise GoogleDriveMalformedPayloadError("Google Drive upload completion state did not validate")
        if size is not None and backup.size != size:
            raise GoogleDriveMalformedPayloadError("Google Drive upload size did not validate")
        return backup

    def _metadata_from_properties(self, properties: Mapping[object, object]) -> BackupObjectMetadata:
        if self._required_string(properties, "cognolith") != "backup":
            raise GoogleDriveMalformedPayloadError("Google Drive file is not a Cognolith backup")
        try:
            drive_owner_id = UUID(self._required_string(properties, "drive_owner_id"))
            workspace_owner_id = UUID(self._required_string(properties, "workspace_owner_id"))
            backup_id = UUID(self._required_string(properties, "backup_id"))
        except ValueError as error:
            raise GoogleDriveMalformedPayloadError("Google Drive backup identifiers are invalid") from error
        raw_schema_version = self._required_string(properties, "schema_version")
        if not raw_schema_version.isdecimal() or int(raw_schema_version) < 1:
            raise GoogleDriveMalformedPayloadError("Google Drive backup schema version is invalid")
        checksum = self._required_string(properties, "archive_checksum")
        if not self._CHECKSUM.fullmatch(checksum):
            raise GoogleDriveMalformedPayloadError("Google Drive backup checksum is invalid")
        app_version = self._optional_app_version(properties.get("app_version"))
        item_counts = self._optional_item_counts(properties.get("item_counts"))
        return BackupObjectMetadata(
            drive_owner_id=drive_owner_id,
            workspace_owner_id=workspace_owner_id,
            backup_id=backup_id,
            schema_version=int(raw_schema_version),
            archive_checksum=checksum,
            created_at=self._parse_datetime(self._required_string(properties, "created_at")),
            app_version=app_version,
            item_counts=item_counts,
        )

    def _app_properties(self, metadata: BackupObjectMetadata, *, completed: bool) -> dict[str, str]:
        properties = {
            "cognolith": "backup",
            "drive_owner_id": str(metadata.drive_owner_id),
            "workspace_owner_id": str(metadata.workspace_owner_id),
            "backup_id": str(metadata.backup_id),
            "schema_version": str(metadata.schema_version),
            "archive_checksum": metadata.archive_checksum,
            "created_at": metadata.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "completed": str(completed).lower(),
        }
        if metadata.app_version is not None:
            properties["app_version"] = metadata.app_version
        if metadata.item_counts:
            compact_counts = {
                self._COUNT_KEYS[key]: value
                for key, value in metadata.item_counts.items()
            }
            properties["item_counts"] = json.dumps(
                compact_counts,
                sort_keys=True,
                separators=(",", ":"),
            )
        return properties

    @classmethod
    def _list_query(cls, drive_owner_id: UUID) -> str:
        return (
            f"appProperties has {{ key='cognolith' and value='{cls._escape_query_value('backup')}' }} "
            "and appProperties has { key='drive_owner_id' "
            f"and value='{cls._escape_query_value(str(drive_owner_id))}' }} "
            "and trashed = false"
        )

    @staticmethod
    def _escape_query_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _required_string(payload: Mapping[object, object], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise GoogleDriveMalformedPayloadError(f"Google Drive file {key} is invalid")
        return value

    def _completed_property(self, properties: Mapping[object, object]) -> bool:
        value = self._required_string(properties, "completed")
        if value == "true":
            return True
        if value == "false":
            return False
        raise GoogleDriveMalformedPayloadError("Google Drive completion property is invalid")

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise GoogleDriveMalformedPayloadError("Google Drive timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GoogleDriveMalformedPayloadError("Google Drive timestamp must include a timezone")
        return parsed.astimezone(UTC)

    def _validate_metadata(self, metadata: BackupObjectMetadata) -> None:
        if metadata.schema_version < 1 or not self._CHECKSUM.fullmatch(metadata.archive_checksum):
            raise ValueError("Backup metadata is invalid")
        if metadata.created_at.tzinfo is None or metadata.created_at.utcoffset() is None:
            raise ValueError("Backup metadata timestamp must include a timezone")
        if (
            metadata.app_version is not None
            and not self._APP_VERSION.fullmatch(metadata.app_version)
        ):
            raise ValueError("Backup metadata app version is invalid")
        self._validate_item_counts(metadata.item_counts, ValueError)

    def _optional_app_version(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not self._APP_VERSION.fullmatch(value):
            raise GoogleDriveMalformedPayloadError(
                "Google Drive backup app version is invalid"
            )
        return value

    def _optional_item_counts(self, value: object) -> dict[str, int]:
        if value is None:
            return {}
        if (
            not isinstance(value, str)
            or len(value) > self._MAX_ITEM_COUNTS_PROPERTY_LENGTH
        ):
            raise GoogleDriveMalformedPayloadError(
                "Google Drive backup item counts are invalid"
            )
        try:
            compact = json.loads(value, object_pairs_hook=self._unique_json_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise GoogleDriveMalformedPayloadError(
                "Google Drive backup item counts are invalid"
            ) from error
        if not isinstance(compact, dict) or any(
            key not in self._COUNT_ALIASES for key in compact
        ):
            raise GoogleDriveMalformedPayloadError(
                "Google Drive backup item counts are invalid"
            )
        counts = {
            self._COUNT_ALIASES[key]: count for key, count in compact.items()
        }
        self._validate_item_counts(
            counts,
            GoogleDriveMalformedPayloadError,
        )
        return counts

    def _validate_item_counts(
        self,
        counts: Mapping[str, object],
        error_type: type[ValueError] | type[GoogleDriveMalformedPayloadError],
    ) -> None:
        if (
            len(counts) > len(self._COUNT_KEYS)
            or any(key not in self._COUNT_KEYS for key in counts)
            or any(
                type(count) is not int or count < 0 or count > 2_147_483_647
                for count in counts.values()
            )
        ):
            raise error_type("Google Drive backup item counts are invalid")
        encoded = json.dumps(
            {self._COUNT_KEYS[key]: count for key, count in counts.items()},
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded) > self._MAX_ITEM_COUNTS_PROPERTY_LENGTH:
            raise error_type("Google Drive backup item counts are invalid")

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("Duplicate item count key")
            output[key] = value
        return output

    def _require_trusted_remote_id(self, remote_id: str) -> None:
        self._validate_remote_id(remote_id)
        if remote_id not in self._trusted_remote_ids:
            raise InvalidGoogleDriveRemoteIdError("Google Drive file ID is not authorized")

    def _validate_remote_id(self, remote_id: str) -> None:
        if not self._REMOTE_ID.fullmatch(remote_id):
            raise InvalidGoogleDriveRemoteIdError("Google Drive file ID is invalid")

    @staticmethod
    def _is_resumable_location(location: str | None) -> bool:
        if not location:
            return False
        parsed = urlparse(location)
        return parsed.scheme == "https" and bool(parsed.netloc)

    @staticmethod
    def _unlink_after_failure(temporary: Path) -> None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    async def _stream_file(self, archive: Path) -> AsyncIterator[bytes]:
        with archive.open("rb") as source:
            while chunk := source.read(self._CHUNK_SIZE):
                yield chunk

    @staticmethod
    def _json_object(response: httpx.Response) -> Mapping[object, object]:
        try:
            payload = response.json()
        except ValueError as error:
            raise GoogleDriveMalformedPayloadError("Google Drive response is not JSON") from error
        if not isinstance(payload, Mapping):
            raise GoogleDriveMalformedPayloadError("Google Drive response is invalid")
        return payload
