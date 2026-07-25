from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from sqlalchemy import delete, or_, text
from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.backup import (
    BackupOperationKind,
    BackupStatus,
    WorkspaceBackup,
)
from app.models.chat import ChatMessages, ChatRole, ChatSession
from app.models.document import Document, DocumentChunks
from app.models.note import (
    NoteCategory,
    NoteCollaborators,
    NoteFolders,
    NoteLinks,
    NoteLinkType,
    Notes,
    NoteTagRelations,
    NoteTags,
    NoteTemplates,
)
from app.models.user import (
    LlmProvider,
    NotesViewMode,
    User,
    UserSettings,
    UserTheme,
)
from app.schemas.backup import BackupPreview, RestoreResult
from app.services.backup_exporter import RECORD_FILENAMES

logger = logging.getLogger(__name__)


class UnsafeBackupArchiveError(RuntimeError):
    """A backup archive failed validation and must not be restored."""


class BackupRestoreFailedError(RuntimeError):
    """A validated backup could not replace the workspace atomically."""


UnsafeBackupArchive = UnsafeBackupArchiveError
BackupRestoreFailed = BackupRestoreFailedError


@dataclass(frozen=True)
class _ValidatedArchive:
    backup_id: UUID
    created_at: datetime
    schema_version: int
    app_version: str
    counts: dict[str, int]
    checksums: dict[str, str]
    records: dict[str, list[dict[str, Any]]]
    document_entries: dict[UUID, str]


@dataclass(frozen=True)
class _OpenedDestination:
    path: Path
    descriptor: int
    parent_descriptor: int | None


_MANIFEST_FIELDS = {
    "schema_version",
    "backup_id",
    "owner_id",
    "created_at",
    "app_version",
    "counts",
    "checksums",
    "record_filenames",
}
_RECORD_FIELDS = {
    "notes": {
        "id",
        "created_at",
        "updated_at",
        "folder_id",
        "title",
        "content",
        "content_type",
        "keywords",
        "ai_generated",
        "is_favorite",
        "is_archived",
        "is_pinned",
        "color",
        "emoji",
        "linked_document_id",
        "linked_chat_session_id",
        "parent_note_id",
        "version",
        "previous_version_id",
        "is_public",
        "is_deleted",
        "last_edited_at",
    },
    "folders": {
        "id",
        "created_at",
        "updated_at",
        "name",
        "description",
        "parent_folder_id",
        "color",
        "icon",
        "emoji",
        "is_shared",
        "is_archived",
        "sort_order",
        "is_deleted",
    },
    "tags": {"id", "name", "color", "description", "created_at"},
    "note_tag_relations": {"note_id", "tag_id", "created_at"},
    "links": {
        "id",
        "source_note_id",
        "target_note_id",
        "link_type",
        "description",
        "created_at",
    },
    "templates": {
        "id",
        "name",
        "description",
        "category",
        "content",
        "content_type",
        "is_public",
        "is_system",
        "created_at",
        "updated_at",
    },
    "documents": {
        "id",
        "created_at",
        "updated_at",
        "title",
        "file_name",
        "file_size",
        "file_type",
        "mime_type",
        "tags",
        "language",
        "is_deleted",
    },
    "chat_sessions": {
        "id",
        "created_at",
        "updated_at",
        "title",
        "description",
        "is_archived",
        "is_pinned",
        "last_message_at",
    },
    "chat_messages": {"id", "created_at", "updated_at", "session_id", "role", "content"},
    "user_preferences": {
        "llm_provider",
        "llm_model",
        "embedding_model",
        "chunk_size",
        "chunk_overlap",
        "top_k_results",
        "similarity_threshold",
        "temperature",
        "max_tokens",
        "theme",
        "language",
        "notes_view_mode",
        "default_note_folder_id",
        "email_notifications",
        "processing_notifications",
        "rag_diagnostics_enabled",
        "created_at",
        "updated_at",
    },
}
_UUID_FIELDS = {
    "notes": {
        "id",
        "folder_id",
        "linked_document_id",
        "linked_chat_session_id",
        "parent_note_id",
        "previous_version_id",
    },
    "folders": {"id", "parent_folder_id"},
    "tags": {"id"},
    "note_tag_relations": {"note_id", "tag_id"},
    "links": {"id", "source_note_id", "target_note_id"},
    "templates": {"id"},
    "documents": {"id"},
    "chat_sessions": {"id"},
    "chat_messages": {"id", "session_id"},
    "user_preferences": {"default_note_folder_id"},
}
_DATETIME_FIELDS = {
    "notes": {"created_at", "updated_at", "last_edited_at"},
    "folders": {"created_at", "updated_at"},
    "tags": {"created_at"},
    "note_tag_relations": {"created_at"},
    "links": {"created_at"},
    "templates": {"created_at", "updated_at"},
    "documents": {"created_at", "updated_at"},
    "chat_sessions": {"created_at", "updated_at", "last_message_at"},
    "chat_messages": {"created_at", "updated_at"},
    "user_preferences": {"created_at", "updated_at"},
}
_BOOL_FIELDS = {
    "notes": {
        "ai_generated",
        "is_favorite",
        "is_archived",
        "is_pinned",
        "is_public",
        "is_deleted",
    },
    "folders": {"is_shared", "is_archived", "is_deleted"},
    "templates": {"is_public", "is_system"},
    "documents": {"is_deleted"},
    "chat_sessions": {"is_archived", "is_pinned"},
    "user_preferences": {
        "email_notifications",
        "processing_notifications",
        "rag_diagnostics_enabled",
    },
}
_INT_FIELDS = {
    "notes": {"version"},
    "folders": {"sort_order"},
    "documents": {"file_size"},
    "user_preferences": {
        "chunk_size",
        "chunk_overlap",
        "top_k_results",
        "max_tokens",
    },
}
_FLOAT_FIELDS = {
    "user_preferences": {"similarity_threshold", "temperature"},
}
_LIST_STRING_FIELDS = {
    "notes": {"keywords"},
    "documents": {"tags"},
}
_NULLABLE_FIELDS = {
    "notes": {
        "folder_id",
        "color",
        "emoji",
        "linked_document_id",
        "linked_chat_session_id",
        "parent_note_id",
        "previous_version_id",
        "last_edited_at",
    },
    "folders": {"description", "parent_folder_id", "color", "icon", "emoji"},
    "tags": {"color", "description"},
    "links": {"description"},
    "templates": {"description"},
    "chat_sessions": {"title", "description", "last_message_at"},
    "user_preferences": {"default_note_folder_id"},
}
_ENUM_FIELDS = {
    ("links", "link_type"): {"related", "referenced", "parent", "child"},
    ("templates", "category"): {"personal", "meeting", "work", "study", "research", "other"},
    ("chat_messages", "role"): {"user", "assistant", "system"},
    ("user_preferences", "llm_provider"): {
        "openai",
        "anthropic",
        "ollama",
        "gemini",
        "huggingface",
        "custom",
    },
    ("user_preferences", "theme"): {"light", "dark", "auto"},
    ("user_preferences", "notes_view_mode"): {"grid", "list"},
}
_MAX_STRING_LENGTHS = {
    ("notes", "title"): 500,
    ("notes", "content_type"): 20,
    ("notes", "color"): 20,
    ("notes", "emoji"): 10,
    ("folders", "name"): 255,
    ("folders", "color"): 20,
    ("folders", "icon"): 50,
    ("folders", "emoji"): 10,
    ("tags", "name"): 100,
    ("tags", "color"): 20,
    ("templates", "name"): 255,
    ("templates", "content_type"): 20,
    ("documents", "title"): 255,
    ("documents", "file_name"): 255,
    ("documents", "file_type"): 255,
    ("documents", "mime_type"): 255,
    ("documents", "language"): 10,
    ("chat_sessions", "title"): 255,
    ("user_preferences", "llm_model"): 100,
    ("user_preferences", "embedding_model"): 100,
    ("user_preferences", "language"): 10,
}


class BackupImporter:
    _GLOBAL_RESTORE_LOCK_NAMESPACE = 7_327_502
    _GLOBAL_RESTORE_LOCK_ID = 1

    def __init__(
        self,
        *,
        maximum_archive_size: int | None = None,
        maximum_expanded_size: int | None = None,
        maximum_entry_size: int | None = None,
        maximum_entry_count: int = 10_000,
        maximum_compression_ratio: int = 200,
        maximum_json_depth: int = 32,
        maximum_json_string_length: int = 1_000_000,
        maximum_json_collection_size: int = 1_000_000,
        supported_app_versions: set[str] | None = None,
        upload_root: Path | None = None,
        temporary_directory: Path | None = None,
        session: Session | None = None,
        move_file: Callable[[Path, Path], None] | None = None,
    ) -> None:
        archive_limit = maximum_archive_size or settings.BACKUP_MAX_ARCHIVE_SIZE
        self.maximum_archive_size = archive_limit
        self.maximum_expanded_size = maximum_expanded_size or archive_limit * 4
        self.maximum_entry_size = maximum_entry_size or archive_limit
        self.maximum_entry_count = maximum_entry_count
        self.maximum_compression_ratio = maximum_compression_ratio
        self.maximum_json_depth = maximum_json_depth
        self.maximum_json_string_length = maximum_json_string_length
        self.maximum_json_collection_size = maximum_json_collection_size
        self.supported_app_versions = supported_app_versions or {settings.VERSION}
        self.upload_root = Path(upload_root or settings.UPLOAD_DIR)
        self.temporary_directory = Path(temporary_directory or settings.BACKUP_TEMP_DIR)
        self.session = session
        self.move_file = move_file

    def preview(
        self,
        path: Path,
        expected_workspace_owner_id: UUID,
        expected_archive_backup_id: UUID | None = None,
    ) -> BackupPreview:
        archive_path = Path(path)
        validated = self._validate(
            archive_path,
            expected_workspace_owner_id,
            expected_archive_backup_id,
        )
        return BackupPreview(
            created_at=validated.created_at,
            schema_version=validated.schema_version,
            app_version=validated.app_version,
            archive_size_bytes=archive_path.stat().st_size,
            item_counts=validated.counts,
            warnings=["Document-derived content will be rebuilt after restore."],
        )

    def restore(
        self,
        path: Path,
        user: User,
        expected_workspace_owner_id: UUID,
        *,
        expected_archive_backup_id: UUID | None = None,
        operation: WorkspaceBackup,
        completed_at: datetime,
    ) -> RestoreResult:
        if self.session is None or user.id is None:
            raise BackupRestoreFailed("Restore requires a persisted user and database session")
        self._validate_restore_operation(operation, user.id)
        if not self._try_global_restore_lock():
            self.session.rollback()
            raise BackupRestoreFailed("Another workspace restore is in progress")
        archive_path = Path(path)
        try:
            validated = self._validate(
                archive_path,
                expected_workspace_owner_id,
                expected_archive_backup_id,
            )
        except BaseException:
            self.session.rollback()
            raise

        restore_directory: Path | None = None
        journal: Path | None = None
        final_paths: list[Path] = []
        old_paths: list[str] = []
        try:
            self._recover_incomplete_restores()
            restore_directory = self._create_restore_directory()
            staged = self._stage_documents(archive_path, validated, restore_directory)
            planned = self._plan_final_paths(user.id, validated)
            journal = restore_directory / "recovery-journal.json"
            self._write_recovery_journal(journal, list(planned.values()))
            self._move_staged_documents(staged, planned, final_paths)

            old_paths = self._old_document_paths(user.id)
            self._delete_workspace(user.id)
            reprocessing_ids = self._insert_workspace(user.id, validated, planned)
            operation.status = BackupStatus.completed
            operation.completed_at = completed_at
            operation.failure_message = None
            operation.schema_version = validated.schema_version
            operation.archive_size_bytes = archive_path.stat().st_size
            operation.item_counts = dict(validated.counts)
            self.session.add(operation)
            self.session.commit()
        except BaseException as error:
            self.session.rollback()
            self._remove_new_files(final_paths)
            if restore_directory is not None:
                shutil.rmtree(restore_directory, ignore_errors=True)
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            raise BackupRestoreFailed("Workspace restore failed") from error

        if journal is not None:
            try:
                journal.unlink(missing_ok=True)
            except OSError:
                pass
        if restore_directory is not None:
            shutil.rmtree(restore_directory, ignore_errors=True)
        self._delete_old_files(old_paths)
        return RestoreResult(reprocessing_document_ids=reprocessing_ids)

    @staticmethod
    def _validate_restore_operation(
        operation: WorkspaceBackup, user_id: int
    ) -> None:
        if (
            operation.id is None
            or operation.user_id != user_id
            or operation.operation_kind != BackupOperationKind.restore
            or operation.status != BackupStatus.pending
        ):
            raise BackupRestoreFailed("Restore operation is invalid")

    def _try_global_restore_lock(self) -> bool:
        if self.session is None:
            return False
        return bool(
            self.session.connection()
            .execute(
                text(
                    "SELECT pg_try_advisory_xact_lock(:namespace, :lock_id)"
                ),
                {
                    "namespace": self._GLOBAL_RESTORE_LOCK_NAMESPACE,
                    "lock_id": self._GLOBAL_RESTORE_LOCK_ID,
                },
            )
            .scalar_one()
        )

    def _validate(
        self,
        path: Path,
        expected_workspace_owner_id: UUID,
        expected_archive_backup_id: UUID | None = None,
    ) -> _ValidatedArchive:
        self._validate_archive_file(path)
        try:
            with ZipFile(path) as archive:
                entries = self._validate_entries(archive)
                manifest = self._read_json(archive, entries["manifest.json"])
                validated_manifest = self._validate_manifest(
                    manifest,
                    expected_workspace_owner_id,
                    set(entries) - {"manifest.json"},
                    expected_archive_backup_id,
                )
                records = self._read_records(archive, entries, validated_manifest)
                document_entries = self._validate_records(records, entries)
                self._verify_checksums(archive, entries, validated_manifest["checksums"])
        except (BadZipFile, OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, UnsafeBackupArchive):
                raise
            raise UnsafeBackupArchive("Backup archive is invalid") from error
        return _ValidatedArchive(
            backup_id=validated_manifest["backup_id"],
            created_at=validated_manifest["created_at"],
            schema_version=validated_manifest["schema_version"],
            app_version=validated_manifest["app_version"],
            counts=validated_manifest["counts"],
            checksums=validated_manifest["checksums"],
            records=records,
            document_entries=document_entries,
        )

    def _validate_archive_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
            with path.open("rb") as source:
                signature = source.read(4)
        except OSError as error:
            raise UnsafeBackupArchive("Backup archive is unavailable") from error
        if size < 4 or size > self.maximum_archive_size:
            raise UnsafeBackupArchive("Backup archive size is unsafe")
        if signature not in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
            raise UnsafeBackupArchive("Backup archive signature is invalid")

    def _validate_entries(self, archive: ZipFile) -> dict[str, ZipInfo]:
        infos = archive.infolist()
        if len(infos) > self.maximum_entry_count:
            raise UnsafeBackupArchive("Backup archive has too many entries")
        entries: dict[str, ZipInfo] = {}
        normalized_names: set[str] = set()
        expanded_size = 0
        for info in infos:
            raw_name = self._safe_entry_name(info.orig_filename)
            name = self._safe_entry_name(info.filename)
            if raw_name != name:
                raise UnsafeBackupArchive(
                    "Backup archive raw path is unsafe"
                )
            normalized = unicodedata.normalize("NFKC", name).casefold()
            if normalized in normalized_names:
                raise UnsafeBackupArchive("Backup archive contains duplicate entries")
            normalized_names.add(normalized)
            if name in entries:
                raise UnsafeBackupArchive("Backup archive contains duplicate entries")
            self._validate_entry_type(info)
            if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
                raise UnsafeBackupArchive("Backup archive compression is unsupported")
            if info.file_size < 0 or info.file_size > self.maximum_entry_size:
                raise UnsafeBackupArchive("Backup archive entry is too large")
            expanded_size += info.file_size
            if expanded_size > self.maximum_expanded_size:
                raise UnsafeBackupArchive("Backup archive expands beyond the configured limit")
            if info.file_size and (
                info.compress_size <= 0
                or info.file_size / info.compress_size > self.maximum_compression_ratio
            ):
                raise UnsafeBackupArchive("Backup archive compression ratio is unsafe")
            entries[name] = info
        if "manifest.json" not in entries:
            raise UnsafeBackupArchive("Backup archive manifest is missing")
        return entries

    @staticmethod
    def _safe_entry_name(name: str) -> str:
        if "\x00" in name or "\\" in name:
            raise UnsafeBackupArchive("Backup archive path is unsafe")
        normalized = unicodedata.normalize("NFKC", name)
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or pure.is_absolute()
            or ".." in pure.parts
            or "." in pure.parts
            or any(not part for part in normalized.split("/"))
            or (len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":")
        ):
            raise UnsafeBackupArchive("Backup archive path is unsafe")
        return normalized

    @staticmethod
    def _validate_entry_type(info: ZipInfo) -> None:
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if info.is_dir() or (file_type not in {0, stat.S_IFREG}):
            raise UnsafeBackupArchive("Backup archive entry type is unsafe")
        if mode & 0o111:
            raise UnsafeBackupArchive("Executable backup archive entries are not allowed")

    def _read_json(self, archive: ZipFile, info: ZipInfo) -> Any:
        try:
            with archive.open(info) as source:
                payload = source.read(self.maximum_entry_size + 1)
            if len(payload) > self.maximum_entry_size:
                raise UnsafeBackupArchive("Backup JSON entry is too large")
            value = json.loads(
                payload,
                object_pairs_hook=self._unique_object,
                parse_constant=self._reject_json_constant,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UnsafeBackupArchive("Backup JSON entry is invalid") from error
        self._validate_json_bounds(value)
        return value

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise UnsafeBackupArchive("Backup JSON contains duplicate object keys")
            output[key] = value
        return output

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise UnsafeBackupArchive("Backup JSON number is invalid")

    def _validate_json_bounds(self, value: Any, depth: int = 0) -> None:
        if depth > self.maximum_json_depth:
            raise UnsafeBackupArchive("Backup JSON nesting is too deep")
        if isinstance(value, str):
            if "\x00" in value:
                raise UnsafeBackupArchive("Backup JSON string contains a NUL")
            if len(value) > self.maximum_json_string_length:
                raise UnsafeBackupArchive("Backup JSON string is too long")
            return
        if isinstance(value, list):
            if len(value) > self.maximum_json_collection_size:
                raise UnsafeBackupArchive("Backup JSON collection is too large")
            for item in value:
                self._validate_json_bounds(item, depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > self.maximum_json_collection_size:
                raise UnsafeBackupArchive("Backup JSON collection is too large")
            for key, item in value.items():
                self._validate_json_bounds(key, depth + 1)
                self._validate_json_bounds(item, depth + 1)
            return
        if value is not None and type(value) not in {bool, int, float}:
            raise UnsafeBackupArchive("Backup JSON value type is unsupported")

    def _validate_manifest(
        self,
        value: Any,
        expected_owner_id: UUID,
        entry_names: set[str],
        expected_backup_id: UUID | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
            raise UnsafeBackupArchive("Backup manifest shape is invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise UnsafeBackupArchive("Backup schema version is unsupported")
        backup_id = self._uuid(value["backup_id"])
        if expected_backup_id is not None and backup_id != expected_backup_id:
            raise UnsafeBackupArchive(
                "Backup archive identity does not match trusted metadata"
            )
        owner_id = self._uuid(value["owner_id"])
        if owner_id != expected_owner_id:
            raise UnsafeBackupArchive("Backup workspace owner does not match trusted metadata")
        created_at = self._datetime(value["created_at"])
        app_version = value["app_version"]
        if not isinstance(app_version, str) or app_version not in self.supported_app_versions:
            raise UnsafeBackupArchive("Backup application version is unsupported")
        if value["record_filenames"] != RECORD_FILENAMES:
            raise UnsafeBackupArchive("Backup record filenames are invalid")
        counts = value["counts"]
        if (
            not isinstance(counts, dict)
            or set(counts) != set(RECORD_FILENAMES)
            or any(type(count) is not int or count < 0 for count in counts.values())
        ):
            raise UnsafeBackupArchive("Backup record counts are invalid")
        checksums = value["checksums"]
        if (
            not isinstance(checksums, dict)
            or set(checksums) != entry_names
            or any(
                not isinstance(name, str)
                or not isinstance(checksum, str)
                or len(checksum) != 64
                or any(character not in "0123456789abcdef" for character in checksum)
                for name, checksum in checksums.items()
            )
        ):
            raise UnsafeBackupArchive("Backup entry checksums are invalid")
        return {
            "schema_version": 1,
            "backup_id": backup_id,
            "owner_id": owner_id,
            "created_at": created_at,
            "app_version": app_version,
            "counts": dict(counts),
            "checksums": dict(checksums),
        }

    def _read_records(
        self, archive: ZipFile, entries: Mapping[str, ZipInfo], manifest: Mapping[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        records: dict[str, list[dict[str, Any]]] = {}
        for entity, filename in RECORD_FILENAMES.items():
            info = entries.get(filename)
            if info is None:
                raise UnsafeBackupArchive("Required backup record file is missing")
            value = self._read_json(archive, info)
            if not isinstance(value, list) or len(value) != manifest["counts"][entity]:
                raise UnsafeBackupArchive("Backup record count does not match the manifest")
            if any(not isinstance(record, dict) for record in value):
                raise UnsafeBackupArchive("Backup record shape is invalid")
            records[entity] = value
        return records

    def _validate_records(
        self, records: dict[str, list[dict[str, Any]]], entries: Mapping[str, ZipInfo]
    ) -> dict[UUID, str]:
        identifiers: dict[str, set[UUID]] = {}
        for entity, entity_records in records.items():
            seen: set[UUID] = set()
            for record in entity_records:
                self._validate_record(entity, record)
                if "id" in record:
                    identifier = self._uuid(record["id"])
                    if identifier in seen:
                        raise UnsafeBackupArchive("Backup contains a duplicate portable UUID")
                    seen.add(identifier)
            identifiers[entity] = seen
        if len(records["user_preferences"]) > 1:
            raise UnsafeBackupArchive("Backup contains multiple preference records")
        self._validate_relationships(records, identifiers)
        return self._validate_document_entries(records["documents"], entries)

    def _validate_record(self, entity: str, record: dict[str, Any]) -> None:
        if set(record) != _RECORD_FIELDS[entity]:
            raise UnsafeBackupArchive("Backup record fields are invalid")
        nullable = _NULLABLE_FIELDS.get(entity, set())
        for field, value in record.items():
            if value is None:
                if field not in nullable:
                    raise UnsafeBackupArchive("Backup record contains an invalid null")
                continue
            if field in _UUID_FIELDS.get(entity, set()):
                self._uuid(value)
            elif field in _DATETIME_FIELDS.get(entity, set()):
                self._datetime(value)
            elif field in _BOOL_FIELDS.get(entity, set()):
                if type(value) is not bool:
                    raise UnsafeBackupArchive("Backup record boolean is invalid")
            elif field in _INT_FIELDS.get(entity, set()):
                if type(value) is not int:
                    raise UnsafeBackupArchive("Backup record integer is invalid")
            elif field in _FLOAT_FIELDS.get(entity, set()):
                if type(value) not in {int, float}:
                    raise UnsafeBackupArchive("Backup record number is invalid")
            elif field in _LIST_STRING_FIELDS.get(entity, set()):
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise UnsafeBackupArchive("Backup record string collection is invalid")
            elif not isinstance(value, str):
                raise UnsafeBackupArchive("Backup record string is invalid")
            allowed = _ENUM_FIELDS.get((entity, field))
            if allowed is not None and value not in allowed:
                raise UnsafeBackupArchive("Backup record enumeration is invalid")
            maximum_length = _MAX_STRING_LENGTHS.get((entity, field))
            if maximum_length is not None:
                if not isinstance(value, str) or len(value) > maximum_length:
                    raise UnsafeBackupArchive("Backup record string is too long")
        if entity == "documents" and record["file_size"] < 0:
            raise UnsafeBackupArchive("Backup document size is invalid")
        if entity == "notes" and record["version"] < 1:
            raise UnsafeBackupArchive("Backup note version is invalid")
        if entity == "user_preferences":
            self._validate_preference_domains(record)

    @staticmethod
    def _validate_preference_domains(record: Mapping[str, Any]) -> None:
        if not 100 <= record["chunk_size"] <= 4000:
            raise UnsafeBackupArchive("Backup preference chunk size is invalid")
        if not 0 <= record["chunk_overlap"] <= 1000:
            raise UnsafeBackupArchive("Backup preference chunk overlap is invalid")
        if not 1 <= record["top_k_results"] <= 20:
            raise UnsafeBackupArchive("Backup preference result count is invalid")
        if not 100 <= record["max_tokens"] <= 4000:
            raise UnsafeBackupArchive("Backup preference token count is invalid")
        for field in ("similarity_threshold", "temperature"):
            value = float(record[field])
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise UnsafeBackupArchive("Backup preference number is invalid")

    def _validate_relationships(
        self, records: Mapping[str, list[dict[str, Any]]], identifiers: Mapping[str, set[UUID]]
    ) -> None:
        relation_targets = {
            ("folders", "parent_folder_id"): "folders",
            ("notes", "folder_id"): "folders",
            ("notes", "linked_document_id"): "documents",
            ("notes", "linked_chat_session_id"): "chat_sessions",
            ("notes", "parent_note_id"): "notes",
            ("notes", "previous_version_id"): "notes",
            ("note_tag_relations", "note_id"): "notes",
            ("note_tag_relations", "tag_id"): "tags",
            ("links", "source_note_id"): "notes",
            ("links", "target_note_id"): "notes",
            ("chat_messages", "session_id"): "chat_sessions",
            ("user_preferences", "default_note_folder_id"): "folders",
        }
        for (entity, field), target_entity in relation_targets.items():
            for record in records[entity]:
                raw_identifier = record[field]
                if raw_identifier is not None and self._uuid(raw_identifier) not in identifiers[target_entity]:
                    raise UnsafeBackupArchive("Backup relationship target is missing")
        self._reject_cycles(records["folders"], "parent_folder_id")
        self._reject_cycles(records["notes"], "parent_note_id")
        self._reject_cycles(records["notes"], "previous_version_id")

    def _reject_cycles(self, records: list[dict[str, Any]], relationship: str) -> None:
        parents = {
            self._uuid(record["id"]): (
                self._uuid(record[relationship]) if record[relationship] is not None else None
            )
            for record in records
        }
        for identifier in parents:
            visited: set[UUID] = set()
            current: UUID | None = identifier
            while current is not None:
                if current in visited:
                    raise UnsafeBackupArchive("Backup relationship contains a cycle")
                visited.add(current)
                current = parents.get(current)

    def _validate_document_entries(
        self, documents: list[dict[str, Any]], entries: Mapping[str, ZipInfo]
    ) -> dict[UUID, str]:
        document_entries: dict[UUID, str] = {}
        expected_names = set(RECORD_FILENAMES.values()) | {"manifest.json"}
        for document in documents:
            identifier = self._uuid(document["id"])
            filename = document["file_name"]
            if (
                not isinstance(filename, str)
                or not filename
                or "/" in filename
                or "\\" in filename
                or filename in {".", ".."}
            ):
                raise UnsafeBackupArchive("Backup document filename is invalid")
            archive_name = f"files/documents/{identifier}/{filename}"
            info = entries.get(archive_name)
            if info is None or info.file_size != document["file_size"]:
                raise UnsafeBackupArchive("Backup document payload does not match its record")
            document_entries[identifier] = archive_name
            expected_names.add(archive_name)
        if set(entries) != expected_names:
            raise UnsafeBackupArchive("Backup archive contains an unknown entry")
        return document_entries

    @staticmethod
    def _verify_checksums(
        archive: ZipFile, entries: Mapping[str, ZipInfo], checksums: Mapping[str, str]
    ) -> None:
        for name, expected in checksums.items():
            digest = hashlib.sha256()
            with archive.open(entries[name]) as source:
                while chunk := source.read(64 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise UnsafeBackupArchive("Backup entry checksum does not match the manifest")

    @staticmethod
    def _uuid(value: Any) -> UUID:
        if not isinstance(value, str):
            raise UnsafeBackupArchive("Backup UUID is invalid")
        try:
            return UUID(value)
        except ValueError as error:
            raise UnsafeBackupArchive("Backup UUID is invalid") from error

    @staticmethod
    def _datetime(value: Any) -> datetime:
        if not isinstance(value, str):
            raise UnsafeBackupArchive("Backup timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise UnsafeBackupArchive("Backup timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise UnsafeBackupArchive("Backup timestamp must include a timezone")
        return parsed

    def _create_restore_directory(self) -> Path:
        self.temporary_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.temporary_directory, 0o700)
        directory = Path(
            tempfile.mkdtemp(prefix="restore-", dir=self.temporary_directory)
        )
        os.chmod(directory, 0o700)
        return directory

    def _stage_documents(
        self,
        archive_path: Path,
        validated: _ValidatedArchive,
        restore_directory: Path,
    ) -> dict[UUID, Path]:
        staged: dict[UUID, Path] = {}
        stage_root = restore_directory / "staged"
        stage_root.mkdir(mode=0o700)
        with ZipFile(archive_path) as archive:
            for identifier, archive_name in validated.document_entries.items():
                destination = stage_root / identifier.hex
                with archive.open(archive_name) as source, destination.open("xb") as output:
                    copied = 0
                    digest = hashlib.sha256()
                    while chunk := source.read(64 * 1024):
                        copied += len(chunk)
                        if copied > self.maximum_entry_size:
                            raise UnsafeBackupArchive("Backup document exceeds its validated size")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                info = archive.getinfo(archive_name)
                if copied != info.file_size:
                    raise UnsafeBackupArchive("Backup document changed during staging")
                if digest.hexdigest() != validated.checksums[archive_name]:
                    raise UnsafeBackupArchive("Backup document checksum changed during staging")
                staged[identifier] = destination
        return staged

    def _plan_final_paths(
        self, user_id: int, validated: _ValidatedArchive
    ) -> dict[UUID, Path]:
        self.upload_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = self.upload_root.resolve(strict=True)
        records = {
            self._uuid(record["id"]): record for record in validated.records["documents"]
        }
        return {
            identifier: root
            / str(user_id)
            / f"{uuid4().hex}-{records[identifier]['file_name']}"
            for identifier in validated.document_entries
        }

    def _write_recovery_journal(self, journal: Path, paths: list[Path]) -> None:
        payload = json.dumps(
            {"created_paths": [str(path.absolute()) for path in paths]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        with journal.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        self._fsync_directory(journal.parent)

    def _move_staged_documents(
        self,
        staged: Mapping[UUID, Path],
        planned: Mapping[UUID, Path],
        created_paths: list[Path],
    ) -> None:
        for identifier, source in staged.items():
            opened = self._open_destination(planned[identifier])
            created_paths.append(opened.path)
            try:
                if self.move_file is None:
                    self._replace_file(source, opened)
                else:
                    self.move_file(source, opened.path)
                self._fsync_final_destination(opened)
            finally:
                os.close(opened.descriptor)
                if opened.parent_descriptor is not None:
                    os.close(opened.parent_descriptor)

    def _open_destination(self, destination: Path) -> _OpenedDestination:
        root = self.upload_root.resolve(strict=True)
        try:
            relative = destination.relative_to(root)
        except ValueError as error:
            raise BackupRestoreFailed(
                "Restore destination is outside the upload directory"
            ) from error
        if not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise BackupRestoreFailed("Restore destination is unsafe")
        parent_parts = relative.parts[:-1]
        if self._supports_directory_descriptors():
            return self._open_destination_at(
                root, parent_parts, relative.parts[-1]
            )
        return self._open_destination_by_path(
            root, parent_parts, relative.parts[-1]
        )

    @staticmethod
    def _supports_directory_descriptors() -> bool:
        return (
            os.open in os.supports_dir_fd
            and os.mkdir in os.supports_dir_fd
            and os.replace in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
            and hasattr(os, "O_NOFOLLOW")
        )

    def _open_destination_at(
        self, root: Path, parent_parts: tuple[str, ...], name: str
    ) -> _OpenedDestination:
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        directory_descriptor = os.open(root, directory_flags)
        try:
            for part in parent_parts:
                try:
                    os.mkdir(
                        part,
                        mode=0o700,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    pass
                next_descriptor = os.open(
                    part,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            parent = root.joinpath(*parent_parts).resolve(strict=True)
            parent.relative_to(root)
            descriptor = os.open(
                name,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
        except BaseException:
            os.close(directory_descriptor)
            raise
        return _OpenedDestination(
            path=parent / name,
            descriptor=descriptor,
            parent_descriptor=directory_descriptor,
        )

    @staticmethod
    def _open_destination_by_path(
        root: Path, parent_parts: tuple[str, ...], name: str
    ) -> _OpenedDestination:
        parent = root
        for part in parent_parts:
            candidate = parent / part
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if candidate.is_symlink() or not candidate.is_dir():
                raise BackupRestoreFailed(
                    "Restore destination parent is unsafe"
                )
            parent = candidate.resolve(strict=True)
            try:
                parent.relative_to(root)
            except ValueError as error:
                raise BackupRestoreFailed(
                    "Restore destination parent is outside the upload directory"
                ) from error
        destination = parent / name
        descriptor = os.open(
            destination,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        return _OpenedDestination(
            path=destination,
            descriptor=descriptor,
            parent_descriptor=None,
        )

    @staticmethod
    def _replace_file(
        source: Path, destination: _OpenedDestination
    ) -> None:
        try:
            if destination.parent_descriptor is None:
                os.replace(source, destination.path)
            else:
                os.replace(
                    source,
                    destination.path.name,
                    dst_dir_fd=destination.parent_descriptor,
                )
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            with source.open("rb") as input_file, os.fdopen(
                os.dup(destination.descriptor), "wb"
            ) as output:
                shutil.copyfileobj(input_file, output, length=64 * 1024)
                output.flush()
                os.fsync(output.fileno())
            source.unlink()

    def _fsync_final_destination(
        self, destination: _OpenedDestination
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if destination.parent_descriptor is None:
            descriptor = os.open(destination.path, flags)
        else:
            descriptor = os.open(
                destination.path.name,
                flags,
                dir_fd=destination.parent_descriptor,
            )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        if destination.parent_descriptor is None:
            self._fsync_directory(destination.path.parent)
        else:
            self._fsync_directory_descriptor(
                destination.parent_descriptor
            )

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError as error:
            if self._directory_fsync_is_unsupported(error):
                return
            raise
        try:
            self._fsync_directory_descriptor(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_directory_descriptor(self, descriptor: int) -> None:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if not self._directory_fsync_is_unsupported(error):
                raise

    @staticmethod
    def _directory_fsync_is_unsupported(error: OSError) -> bool:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if os.name == "nt":
            unsupported.update({errno.EACCES, errno.EPERM})
        return error.errno in unsupported

    def _old_document_paths(self, user_id: int) -> list[str]:
        if self.session is None:
            return []
        return list(
            self.session.exec(
                select(Document.file_path).where(Document.user_id == user_id)
            ).all()
        )

    def _delete_workspace(self, user_id: int) -> None:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        note_ids = select(Notes.id).where(Notes.user_id == user_id)
        document_ids = select(Document.id).where(Document.user_id == user_id)
        session_ids = select(ChatSession.id).where(ChatSession.user_id == user_id)
        tag_ids = select(NoteTags.id).where(NoteTags.user_id == user_id)

        self.session.exec(
            delete(NoteCollaborators).where(
                col(NoteCollaborators.note_id).in_(note_ids)
            )
        )
        self.session.exec(
            delete(NoteLinks).where(
                or_(
                    col(NoteLinks.source_note_id).in_(note_ids),
                    col(NoteLinks.target_note_id).in_(note_ids),
                )
            )
        )
        self.session.exec(
            delete(NoteTagRelations).where(
                or_(
                    col(NoteTagRelations.note_id).in_(note_ids),
                    col(NoteTagRelations.tag_id).in_(tag_ids),
                )
            )
        )
        self.session.exec(
            delete(DocumentChunks).where(
                col(DocumentChunks.document_id).in_(document_ids)
            )
        )
        self.session.exec(
            delete(ChatMessages).where(col(ChatMessages.session_id).in_(session_ids))
        )
        self.session.exec(delete(UserSettings).where(col(UserSettings.user_id) == user_id))
        self.session.exec(delete(Notes).where(col(Notes.user_id) == user_id))
        self.session.exec(delete(Document).where(col(Document.user_id) == user_id))
        self.session.exec(delete(ChatSession).where(col(ChatSession.user_id) == user_id))
        self.session.exec(
            delete(NoteTemplates).where(col(NoteTemplates.user_id) == user_id)
        )
        self.session.exec(delete(NoteTags).where(col(NoteTags.user_id) == user_id))
        self.session.exec(delete(NoteFolders).where(col(NoteFolders.user_id) == user_id))
        self.session.flush()

    def _insert_workspace(
        self,
        user_id: int,
        validated: _ValidatedArchive,
        final_paths: Mapping[UUID, Path],
    ) -> list[int]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        records = validated.records
        folders = self._insert_folders(user_id, records["folders"])
        tags = self._insert_tags(user_id, records["tags"])
        documents = self._insert_documents(
            user_id, records["documents"], final_paths
        )
        chats = self._insert_chats(user_id, records["chat_sessions"])
        self._insert_templates(user_id, records["templates"])
        notes = self._insert_notes(
            user_id,
            records["notes"],
            folders=folders,
            documents=documents,
            chats=chats,
        )
        self._insert_note_relations(
            records,
            notes=notes,
            tags=tags,
        )
        self._insert_messages(records["chat_messages"], chats)
        self._insert_preferences(user_id, records["user_preferences"], folders)
        self.session.flush()
        return sorted(
            document.id
            for document in documents.values()
            if document.id is not None and not document.is_deleted
        )

    def _insert_folders(
        self, user_id: int, records: list[dict[str, Any]]
    ) -> dict[UUID, NoteFolders]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        models: dict[UUID, NoteFolders] = {}
        for record in records:
            identifier = self._uuid(record["id"])
            model = NoteFolders(
                portable_id=identifier,
                user_id=user_id,
                created_at=self._datetime(record["created_at"]),
                updated_at=self._datetime(record["updated_at"]),
                name=record["name"],
                description=record["description"],
                color=record["color"],
                icon=record["icon"],
                emoji=record["emoji"],
                is_shared=record["is_shared"],
                is_archived=record["is_archived"],
                sort_order=record["sort_order"],
                is_deleted=record["is_deleted"],
            )
            self.session.add(model)
            models[identifier] = model
        self.session.flush()
        for record in records:
            if record["parent_folder_id"] is not None:
                model = models[self._uuid(record["id"])]
                model.parent_folder_id = models[
                    self._uuid(record["parent_folder_id"])
                ].id
                self.session.add(model)
        self.session.flush()
        return models

    def _insert_tags(
        self, user_id: int, records: list[dict[str, Any]]
    ) -> dict[UUID, NoteTags]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        models: dict[UUID, NoteTags] = {}
        for record in records:
            identifier = self._uuid(record["id"])
            model = NoteTags(
                portable_id=identifier,
                user_id=user_id,
                name=record["name"],
                color=record["color"],
                description=record["description"],
                created_at=self._datetime(record["created_at"]),
            )
            self.session.add(model)
            models[identifier] = model
        self.session.flush()
        return models

    def _insert_documents(
        self,
        user_id: int,
        records: list[dict[str, Any]],
        final_paths: Mapping[UUID, Path],
    ) -> dict[UUID, Document]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        models: dict[UUID, Document] = {}
        for record in records:
            identifier = self._uuid(record["id"])
            model = Document(
                portable_id=identifier,
                user_id=user_id,
                created_at=self._datetime(record["created_at"]),
                updated_at=self._datetime(record["updated_at"]),
                title=record["title"],
                file_name=record["file_name"],
                file_path=str(final_paths[identifier]),
                file_size=record["file_size"],
                file_type=record["file_type"],
                mime_type=record["mime_type"],
                is_deleted=record["is_deleted"],
                content=None,
                content_preview=None,
                summary=None,
                keywords=[],
                tags=record["tags"],
                language=record["language"],
                status="deleted" if record["is_deleted"] else "processing",
                processing_started_at=None,
                processing_completed_at=None,
                processing_error=None,
                word_count=None,
                page_count=None,
                chunk_count=0,
            )
            self.session.add(model)
            models[identifier] = model
        self.session.flush()
        return models

    def _insert_chats(
        self, user_id: int, records: list[dict[str, Any]]
    ) -> dict[UUID, ChatSession]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        models: dict[UUID, ChatSession] = {}
        for record in records:
            identifier = self._uuid(record["id"])
            model = ChatSession(
                portable_id=identifier,
                user_id=user_id,
                created_at=self._datetime(record["created_at"]),
                updated_at=self._datetime(record["updated_at"]),
                title=record["title"],
                description=record["description"],
                is_archived=record["is_archived"],
                is_pinned=record["is_pinned"],
                last_message_at=self._datetime(record["last_message_at"]),
            )
            self.session.add(model)
            models[identifier] = model
        self.session.flush()
        return models

    def _insert_templates(
        self, user_id: int, records: list[dict[str, Any]]
    ) -> None:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        for record in records:
            self.session.add(
                NoteTemplates(
                    portable_id=self._uuid(record["id"]),
                    user_id=user_id,
                    created_at=self._datetime(record["created_at"]),
                    updated_at=self._datetime(record["updated_at"]),
                    name=record["name"],
                    description=record["description"],
                    category=NoteCategory(record["category"]),
                    content=record["content"],
                    content_type=record["content_type"],
                    is_public=record["is_public"],
                    is_system=record["is_system"],
                    usage_count=0,
                )
            )
        self.session.flush()

    def _insert_notes(
        self,
        user_id: int,
        records: list[dict[str, Any]],
        *,
        folders: Mapping[UUID, NoteFolders],
        documents: Mapping[UUID, Document],
        chats: Mapping[UUID, ChatSession],
    ) -> dict[UUID, Notes]:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        models: dict[UUID, Notes] = {}
        for record in records:
            identifier = self._uuid(record["id"])
            model = Notes(
                portable_id=identifier,
                user_id=user_id,
                created_at=self._datetime(record["created_at"]),
                updated_at=self._datetime(record["updated_at"]),
                folder_id=self._mapped_id(folders, record["folder_id"]),
                title=record["title"],
                content=record["content"],
                content_type=record["content_type"],
                content_preview=None,
                summary=None,
                keywords=record["keywords"],
                ai_generated=record["ai_generated"],
                is_favorite=record["is_favorite"],
                is_archived=record["is_archived"],
                is_pinned=record["is_pinned"],
                color=record["color"],
                emoji=record["emoji"],
                linked_document_id=self._mapped_id(
                    documents, record["linked_document_id"]
                ),
                linked_chat_session_id=self._mapped_id(
                    chats, record["linked_chat_session_id"]
                ),
                version=record["version"],
                is_public=record["is_public"],
                is_locked=False,
                is_deleted=record["is_deleted"],
                locked_by=None,
                locked_at=None,
                word_count=None,
                char_count=None,
                read_time_minutes=None,
                last_edited_at=self._datetime(record["last_edited_at"]),
            )
            self.session.add(model)
            models[identifier] = model
        self.session.flush()
        for record in records:
            model = models[self._uuid(record["id"])]
            model.parent_note_id = self._mapped_id(models, record["parent_note_id"])
            model.previous_version_id = self._mapped_id(
                models, record["previous_version_id"]
            )
            self.session.add(model)
        self.session.flush()
        return models

    def _insert_note_relations(
        self,
        records: Mapping[str, list[dict[str, Any]]],
        *,
        notes: Mapping[UUID, Notes],
        tags: Mapping[UUID, NoteTags],
    ) -> None:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        for record in records["note_tag_relations"]:
            self.session.add(
                NoteTagRelations(
                    note_id=self._required_id(notes, record["note_id"]),
                    tag_id=self._required_id(tags, record["tag_id"]),
                    created_at=self._datetime(record["created_at"]),
                )
            )
        for record in records["links"]:
            self.session.add(
                NoteLinks(
                    portable_id=self._uuid(record["id"]),
                    source_note_id=self._required_id(
                        notes, record["source_note_id"]
                    ),
                    target_note_id=self._required_id(
                        notes, record["target_note_id"]
                    ),
                    link_type=NoteLinkType(record["link_type"]),
                    description=record["description"],
                    created_at=self._datetime(record["created_at"]),
                )
            )

    def _insert_messages(
        self,
        records: list[dict[str, Any]],
        chats: Mapping[UUID, ChatSession],
    ) -> None:
        if self.session is None:
            raise RuntimeError("Restore session is unavailable")
        for record in records:
            self.session.add(
                ChatMessages(
                    portable_id=self._uuid(record["id"]),
                    session_id=self._required_id(chats, record["session_id"]),
                    created_at=self._datetime(record["created_at"]),
                    updated_at=self._datetime(record["updated_at"]),
                    role=ChatRole(record["role"]),
                    content=record["content"],
                    sources=None,
                    model_used=None,
                    tokens_used=None,
                    response_time_ms=None,
                    rating=None,
                    feedback=None,
                    generation_status=None,
                    generation_error=None,
                    generation_metadata=None,
                    generation_started_at=None,
                    generation_completed_at=None,
                )
            )

    def _insert_preferences(
        self,
        user_id: int,
        records: list[dict[str, Any]],
        folders: Mapping[UUID, NoteFolders],
    ) -> None:
        if self.session is None or not records:
            return
        record = records[0]
        self.session.add(
            UserSettings(
                user_id=user_id,
                llm_provider=LlmProvider(record["llm_provider"]),
                llm_model=record["llm_model"],
                embedding_model=record["embedding_model"],
                chunk_size=record["chunk_size"],
                chunk_overlap=record["chunk_overlap"],
                top_k_results=record["top_k_results"],
                similarity_threshold=record["similarity_threshold"],
                temperature=record["temperature"],
                max_tokens=record["max_tokens"],
                theme=UserTheme(record["theme"]),
                language=record["language"],
                notes_view_mode=NotesViewMode(record["notes_view_mode"]),
                default_note_folder_id=self._mapped_id(
                    folders, record["default_note_folder_id"]
                ),
                email_notifications=record["email_notifications"],
                processing_notifications=record["processing_notifications"],
                rag_diagnostics_enabled=record["rag_diagnostics_enabled"],
                created_at=self._datetime(record["created_at"]),
                updated_at=self._datetime(record["updated_at"]),
            )
        )

    def _recover_incomplete_restores(self) -> None:
        if self.session is None or not self.temporary_directory.exists():
            return
        for journal in self.temporary_directory.glob(
            "restore-*/recovery-journal.json"
        ):
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                paths = payload.get("created_paths", [])
                if not isinstance(paths, list):
                    continue
                for value in paths:
                    if isinstance(value, str):
                        self._delete_if_unreferenced(Path(value))
            except (OSError, ValueError, TypeError):
                continue
            finally:
                shutil.rmtree(journal.parent, ignore_errors=True)

    def _delete_old_files(self, paths: list[str]) -> None:
        for value in set(paths):
            try:
                self._delete_if_unreferenced(Path(value))
            except Exception as error:
                logger.warning(
                    "Post-restore old-file cleanup failed",
                    extra={
                        "cleanup_error_type": type(error).__name__,
                    },
                )
                if self.session is not None:
                    try:
                        self.session.rollback()
                    except Exception as rollback_error:
                        logger.warning(
                            "Post-restore cleanup session reset failed",
                            extra={
                                "cleanup_error_type": (
                                    type(rollback_error).__name__
                                ),
                            },
                        )

    def _delete_if_unreferenced(self, path: Path) -> None:
        if self.session is None:
            return
        confined = self._confined_regular_file(path)
        if confined is None:
            return
        surviving_paths = self.session.exec(select(Document.file_path)).all()
        if not any(
            self._resolved_upload_path(Path(value)) == confined
            for value in surviving_paths
        ):
            confined.unlink()

    def _confined_regular_file(self, path: Path) -> Path | None:
        root = self.upload_root.resolve()
        candidate = path if path.is_absolute() else self.upload_root / path
        try:
            if candidate.is_symlink():
                return None
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not stat.S_ISREG(resolved.stat().st_mode):
                return None
        except (OSError, ValueError):
            return None
        return resolved

    def _resolved_upload_path(self, path: Path) -> Path | None:
        root = self.upload_root.resolve()
        candidate = path if path.is_absolute() else self.upload_root / path
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved

    @staticmethod
    def _remove_new_files(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        for parent in {path.parent for path in paths}:
            try:
                parent.rmdir()
            except OSError:
                continue

    @classmethod
    def _mapped_id(cls, models: Mapping[UUID, Any], value: Any) -> int | None:
        if value is None:
            return None
        return cls._required_id(models, value)

    @classmethod
    def _required_id(cls, models: Mapping[UUID, Any], value: Any) -> int:
        model = models[cls._uuid(value)]
        identifier = model.id
        if identifier is None:
            raise RuntimeError("Restored model was not persisted")
        return identifier


__all__ = [
    "BackupImporter",
    "BackupRestoreFailed",
    "BackupRestoreFailedError",
    "UnsafeBackupArchive",
    "UnsafeBackupArchiveError",
]
