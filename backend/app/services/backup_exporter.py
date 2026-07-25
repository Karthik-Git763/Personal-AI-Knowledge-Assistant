from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, col, select

from app.core.config import settings
from app.models.chat import ChatMessages, ChatSession
from app.models.document import Document
from app.models.note import NoteFolders, NoteLinks, Notes, NoteTagRelations, NoteTags, NoteTemplates
from app.models.user import User, UserSettings
from app.services.backup_archive import (
    BackupExportResult,
    BackupManifestV1,
    VersionedZipWriter,
    canonical_json_bytes,
    open_validated_source,
    safe_archive_filename,
    sha256_file,
    validate_archive_path,
)


class InvalidBackupReferenceError(ValueError):
    """A durable row points at a missing or foreign target."""


InvalidBackupReference = InvalidBackupReferenceError


RECORD_FILENAMES = {
    "notes": "notes.json",
    "folders": "folders.json",
    "tags": "tags.json",
    "note_tag_relations": "note_tag_relations.json",
    "links": "links.json",
    "templates": "templates.json",
    "documents": "documents.json",
    "chat_sessions": "chat_sessions.json",
    "chat_messages": "chat_messages.json",
    "user_preferences": "user_preferences.json",
}


class BackupExporter:
    BATCH_SIZE = 100

    def __init__(
        self,
        *,
        session: Session,
        upload_root: Path | None = None,
        maximum_archive_size: int | None = None,
        app_version: str | None = None,
    ) -> None:
        self.session = session
        self.upload_root = Path(upload_root or settings.UPLOAD_DIR)
        self.maximum_archive_size = (
            maximum_archive_size
            if maximum_archive_size is not None
            else settings.BACKUP_MAX_ARCHIVE_SIZE
        )
        self.app_version = app_version or settings.VERSION
        self._required_note_ids_by_user: dict[int, set[int]] = {}

    def validate_source_path(self, source: Path) -> Path:
        return validate_archive_path(self.upload_root, source)

    def export(
        self,
        user: User,
        destination: Path,
        *,
        backup_id: UUID | None = None,
    ) -> BackupExportResult:
        if user.id is None:
            raise ValueError("Backup exports require a persisted user")

        self._required_note_ids_by_user[user.id] = self._calculate_required_note_ids(user.id)
        default_folder = self._default_preference_folder(user.id)
        archive_backup_id = backup_id or uuid4()
        created_at = datetime.now(UTC)
        checksums: dict[str, str] = {}
        counts: dict[str, int] = {}
        destination = Path(destination)
        temporary_destination = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        manifest: BackupManifestV1 | None = None

        try:
            with VersionedZipWriter(temporary_destination, self.maximum_archive_size) as archive:
                for name, records in self._record_streams(user.id, default_folder):
                    filename = RECORD_FILENAMES[name]
                    checksums[filename], counts[name] = archive.write_json_array(filename, records)
                self._write_document_files(archive, user.id, checksums)
                manifest = BackupManifestV1(
                    schema_version=1,
                    backup_id=archive_backup_id,
                    owner_id=user.portable_id,
                    created_at=created_at,
                    app_version=self.app_version,
                    counts=counts,
                    checksums=checksums,
                    record_filenames=RECORD_FILENAMES,
                )
                archive.write_bytes("manifest.json", canonical_json_bytes(_manifest_record(manifest)))
            temporary_destination.replace(destination)
        except Exception:
            temporary_destination.unlink(missing_ok=True)
            raise
        if manifest is None:
            raise RuntimeError("Backup manifest was not created")

        return BackupExportResult(
            path=destination,
            manifest=manifest,
            archive_checksum=sha256_file(destination),
            archive_size=destination.stat().st_size,
        )

    def _record_streams(
        self, user_id: int, default_folder: NoteFolders | None
    ) -> Iterable[tuple[str, Iterable[Mapping[str, Any]]]]:
        return (
            ("notes", self._iter_notes(user_id)),
            ("folders", self._iter_folders(user_id, default_folder.id if default_folder else None)),
            ("tags", self._iter_tags(user_id)),
            ("note_tag_relations", self._iter_tag_relations(user_id)),
            ("links", self._iter_links(user_id)),
            ("templates", self._iter_templates(user_id)),
            ("documents", self._iter_documents(user_id)),
            ("chat_sessions", self._iter_sessions(user_id)),
            ("chat_messages", self._iter_messages(user_id)),
            ("user_preferences", self._iter_preferences(user_id, default_folder)),
        )

    def _iterate(self, statement: Any) -> Iterator[Any]:
        result = self.session.exec(statement.execution_options(stream_results=True))
        return iter(result.yield_per(self.BATCH_SIZE))

    def _iter_notes(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(Notes).where(Notes.user_id == user_id).order_by(col(Notes.portable_id))
        for note in self._iterate(statement):
            if not note.is_deleted or self._is_required_note(note, user_id):
                yield self._note_record(note, user_id)

    def _iter_folders(
        self, user_id: int, default_folder_id: int | None
    ) -> Iterator[Mapping[str, Any]]:
        statement = select(NoteFolders).where(NoteFolders.user_id == user_id).order_by(col(NoteFolders.portable_id))
        for folder in self._iterate(statement):
            if self._is_required_folder(folder, user_id, default_folder_id):
                yield self._folder_record(folder, user_id)

    def _iter_tags(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(NoteTags).where(NoteTags.user_id == user_id).order_by(col(NoteTags.portable_id))
        for tag in self._iterate(statement):
            yield _tag_record(tag)

    def _iter_tag_relations(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(NoteTagRelations).order_by(
            col(NoteTagRelations.note_id), col(NoteTagRelations.tag_id)
        )
        for relation in self._iterate(statement):
            note = self._get(Notes, relation.note_id, "note tag relation note")
            if note.user_id != user_id or (note.is_deleted and not self._is_required_note(note, user_id)):
                continue
            tag = self._require_owned(NoteTags, relation.tag_id, user_id, "note tag relation tag")
            yield {
                "note_id": str(note.portable_id),
                "tag_id": str(tag.portable_id),
                "created_at": _record_time(relation.created_at),
            }

    def _iter_links(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(NoteLinks).order_by(col(NoteLinks.portable_id))
        for link in self._iterate(statement):
            source = self._get(Notes, link.source_note_id, "link source note")
            if source.user_id != user_id:
                continue
            target = self._require_owned(Notes, link.target_note_id, user_id, "link target note")
            if source.is_deleted and not self._is_required_note(source, user_id):
                continue
            if target.is_deleted and not self._is_required_note(target, user_id):
                continue
            yield {
                "id": str(link.portable_id),
                "source_note_id": str(source.portable_id),
                "target_note_id": str(target.portable_id),
                "link_type": link.link_type.value,
                "description": link.description,
                "created_at": _record_time(link.created_at),
            }

    def _iter_templates(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(NoteTemplates).where(NoteTemplates.user_id == user_id).order_by(
            col(NoteTemplates.portable_id)
        )
        for template in self._iterate(statement):
            yield {
                "id": str(template.portable_id),
                "name": template.name,
                "description": template.description,
                "category": template.category.value if template.category is not None else None,
                "content": template.content,
                "content_type": template.content_type,
                "is_public": template.is_public,
                "is_system": template.is_system,
                "created_at": _record_time(template.created_at),
                "updated_at": _record_time(template.updated_at),
            }

    def _iter_documents(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(Document).where(Document.user_id == user_id).order_by(col(Document.portable_id))
        for document in self._iterate(statement):
            if not document.is_deleted or self._is_referenced_by_live_note("linked_document_id", document.id, user_id):
                yield _document_record(document)

    def _iter_document_models(self, user_id: int) -> Iterator[Document]:
        statement = select(Document).where(Document.user_id == user_id).order_by(col(Document.portable_id))
        for document in self._iterate(statement):
            if not document.is_deleted or self._is_referenced_by_live_note("linked_document_id", document.id, user_id):
                yield document

    def _iter_sessions(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(ChatSession).where(ChatSession.user_id == user_id).order_by(
            col(ChatSession.portable_id)
        )
        for session in self._iterate(statement):
            yield _session_record(session)

    def _iter_messages(self, user_id: int) -> Iterator[Mapping[str, Any]]:
        statement = select(ChatMessages).order_by(col(ChatMessages.portable_id))
        for message in self._iterate(statement):
            session = self._get(ChatSession, message.session_id, "chat message session")
            if session.user_id != user_id:
                continue
            yield {
                "id": str(message.portable_id),
                "created_at": _record_time(message.created_at),
                "updated_at": _record_time(message.updated_at),
                "session_id": str(session.portable_id),
                "role": message.role.value,
                "content": message.content,
            }

    def _iter_preferences(
        self, user_id: int, default_folder: NoteFolders | None
    ) -> Iterator[Mapping[str, Any]]:
        preferences = self.session.get(UserSettings, user_id)
        if preferences is None:
            return
        yield {
            "llm_provider": preferences.llm_provider.value,
            "llm_model": preferences.llm_model,
            "embedding_model": preferences.embedding_model,
            "chunk_size": preferences.chunk_size,
            "chunk_overlap": preferences.chunk_overlap,
            "top_k_results": preferences.top_k_results,
            "similarity_threshold": preferences.similarity_threshold,
            "temperature": preferences.temperature,
            "max_tokens": preferences.max_tokens,
            "theme": preferences.theme.value,
            "language": preferences.language,
            "notes_view_mode": preferences.notes_view_mode.value,
            "default_note_folder_id": str(default_folder.portable_id) if default_folder else None,
            "email_notifications": preferences.email_notifications,
            "processing_notifications": preferences.processing_notifications,
            "rag_diagnostics_enabled": preferences.rag_diagnostics_enabled,
            "created_at": _record_time(preferences.created_at),
            "updated_at": _record_time(preferences.updated_at),
        }

    def _write_document_files(
        self, archive: VersionedZipWriter, user_id: int, checksums: dict[str, str]
    ) -> None:
        for document in self._iter_document_models(user_id):
            archive_name = f"files/documents/{document.portable_id}/{safe_archive_filename(document.file_name)}"
            with open_validated_source(self.upload_root, Path(document.file_path)) as source:
                checksums[archive_name] = archive.write_fileobj(archive_name, source)

    def _note_record(self, note: Notes, user_id: int) -> Mapping[str, Any]:
        folder = self._optional_owned(NoteFolders, note.folder_id, user_id, "note folder")
        document = self._optional_owned(Document, note.linked_document_id, user_id, "note document")
        session = self._optional_owned(
            ChatSession, note.linked_chat_session_id, user_id, "note chat session"
        )
        parent = self._optional_owned(Notes, note.parent_note_id, user_id, "note parent")
        previous = self._optional_owned(Notes, note.previous_version_id, user_id, "note version")
        return {
            "id": str(note.portable_id),
            "created_at": _record_time(note.created_at),
            "updated_at": _record_time(note.updated_at),
            "folder_id": str(folder.portable_id) if folder else None,
            "title": note.title,
            "content": note.content,
            "content_type": note.content_type,
            "keywords": note.keywords,
            "ai_generated": note.ai_generated,
            "is_favorite": note.is_favorite,
            "is_archived": note.is_archived,
            "is_pinned": note.is_pinned,
            "color": note.color,
            "emoji": note.emoji,
            "linked_document_id": str(document.portable_id) if document else None,
            "linked_chat_session_id": str(session.portable_id) if session else None,
            "parent_note_id": str(parent.portable_id) if parent else None,
            "version": note.version,
            "previous_version_id": str(previous.portable_id) if previous else None,
            "is_public": note.is_public,
            "is_deleted": note.is_deleted,
            "last_edited_at": _record_time(note.last_edited_at),
        }

    def _folder_record(self, folder: NoteFolders, user_id: int) -> Mapping[str, Any]:
        parent = self._optional_owned(NoteFolders, folder.parent_folder_id, user_id, "folder parent")
        return {
            "id": str(folder.portable_id),
            "created_at": _record_time(folder.created_at),
            "updated_at": _record_time(folder.updated_at),
            "name": folder.name,
            "description": folder.description,
            "parent_folder_id": str(parent.portable_id) if parent else None,
            "color": folder.color,
            "icon": folder.icon,
            "emoji": folder.emoji,
            "is_shared": folder.is_shared,
            "is_archived": folder.is_archived,
            "sort_order": folder.sort_order,
            "is_deleted": folder.is_deleted,
        }

    def _is_required_note(self, note: Notes, user_id: int) -> bool:
        if note.id is None:
            return False
        required = self._required_note_ids_by_user.get(user_id)
        if required is None:
            required = self._calculate_required_note_ids(user_id)
            self._required_note_ids_by_user[user_id] = required
        return note.id in required

    def _calculate_required_note_ids(self, user_id: int) -> set[int]:
        notes = list(
            self._iterate(
                select(Notes).where(Notes.user_id == user_id).order_by(col(Notes.portable_id))
            )
        )
        notes_by_id = {note.id: note for note in notes if note.id is not None}
        required = {identifier for identifier, note in notes_by_id.items() if not note.is_deleted}

        for link in self._iterate(select(NoteLinks).order_by(col(NoteLinks.portable_id))):
            source = notes_by_id.get(link.source_note_id)
            target = notes_by_id.get(link.target_note_id)
            if source is None and target is None:
                continue
            if source is None or target is None:
                raise InvalidBackupReference("Backup note link belongs to another user")
            if not source.is_deleted or not target.is_deleted:
                required.update((link.source_note_id, link.target_note_id))

        pending = list(required)
        while pending:
            note = notes_by_id[pending.pop()]
            for relationship, target_id in (
                ("parent", note.parent_note_id),
                ("version", note.previous_version_id),
            ):
                if target_id is None or target_id in required:
                    continue
                if target_id not in notes_by_id:
                    raise InvalidBackupReference(
                        f"Backup note {relationship} belongs to another user"
                    )
                required.add(target_id)
                pending.append(target_id)
        return required

    def _is_required_folder(
        self,
        folder: NoteFolders,
        user_id: int,
        default_folder_id: int | None,
        ancestors: set[int] | None = None,
    ) -> bool:
        if folder.id is None:
            return False
        if folder.id == default_folder_id or not folder.is_deleted:
            return True
        if self._is_referenced_by_live_note("folder_id", folder.id, user_id):
            return True
        ancestor_ids = ancestors or set()
        if folder.id in ancestor_ids:
            raise InvalidBackupReference("Backup folder hierarchy contains a cycle")
        child_statement = (
            select(NoteFolders)
            .where(NoteFolders.user_id == user_id, NoteFolders.parent_folder_id == folder.id)
            .order_by(col(NoteFolders.portable_id))
        )
        for child in self._iterate(child_statement):
            if self._is_required_folder(child, user_id, default_folder_id, ancestor_ids | {folder.id}):
                return True
        return False

    def _is_referenced_by_live_note(self, attribute: str, identifier: int | None, user_id: int) -> bool:
        if identifier is None:
            return False
        relationship = getattr(Notes, attribute)
        statement = select(Notes.id).where(
            Notes.user_id == user_id,
            col(Notes.is_deleted).is_(False),
            relationship == identifier,
        )
        return self.session.exec(statement.limit(1)).first() is not None

    def _default_preference_folder(self, user_id: int) -> NoteFolders | None:
        preferences = self.session.get(UserSettings, user_id)
        if preferences is None:
            return None
        return self._optional_owned(
            NoteFolders, preferences.default_note_folder_id, user_id, "default note folder"
        )

    def _get(self, model_type: Any, identifier: int | None, relationship: str) -> Any:
        if identifier is None:
            raise InvalidBackupReference(f"Backup {relationship} is missing")
        target = self.session.get(model_type, identifier)
        if target is None:
            raise InvalidBackupReference(f"Backup {relationship} does not exist")
        return target

    def _require_owned(self, model_type: Any, identifier: int | None, user_id: int, relationship: str) -> Any:
        target = self._get(model_type, identifier, relationship)
        if target.user_id != user_id:
            raise InvalidBackupReference(f"Backup {relationship} belongs to another user")
        return target

    def _optional_owned(
        self, model_type: Any, identifier: int | None, user_id: int, relationship: str
    ) -> Any | None:
        if identifier is None:
            return None
        return self._require_owned(model_type, identifier, user_id, relationship)


def _tag_record(tag: NoteTags) -> Mapping[str, Any]:
    return {
        "id": str(tag.portable_id),
        "name": tag.name,
        "color": tag.color,
        "description": tag.description,
        "created_at": _record_time(tag.created_at),
    }


def _document_record(document: Document) -> Mapping[str, Any]:
    return {
        "id": str(document.portable_id),
        "created_at": _record_time(document.created_at),
        "updated_at": _record_time(document.updated_at),
        "title": document.title,
        "file_name": safe_archive_filename(document.file_name),
        "file_size": document.file_size,
        "file_type": document.file_type,
        "mime_type": document.mime_type,
        "tags": document.tags,
        "language": document.language,
        "is_deleted": document.is_deleted,
    }


def _session_record(session: ChatSession) -> Mapping[str, Any]:
    return {
        "id": str(session.portable_id),
        "created_at": _record_time(session.created_at),
        "updated_at": _record_time(session.updated_at),
        "title": session.title,
        "description": session.description,
        "is_archived": session.is_archived,
        "is_pinned": session.is_pinned,
        "last_message_at": _record_time(session.last_message_at),
    }


def _record_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _manifest_record(manifest: BackupManifestV1) -> Mapping[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "backup_id": str(manifest.backup_id),
        "owner_id": str(manifest.owner_id),
        "created_at": _record_time(manifest.created_at),
        "app_version": manifest.app_version,
        "counts": manifest.counts,
        "checksums": manifest.checksums,
        "record_filenames": manifest.record_filenames,
    }


__all__ = ["BackupExporter", "InvalidBackupReference", "validate_archive_path"]
