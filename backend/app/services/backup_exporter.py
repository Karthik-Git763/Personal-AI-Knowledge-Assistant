from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4, uuid5

from sqlmodel import Session, select

from app.core.config import settings
from app.models.chat import ChatMessages, ChatSession
from app.models.document import Document
from app.models.note import (
    NoteFolders,
    NoteLinks,
    Notes,
    NoteTagRelations,
    NoteTags,
    NoteTemplates,
)
from app.models.user import User, UserSettings
from app.services.backup_archive import (
    BackupExportResult,
    BackupManifestV1,
    VersionedZipWriter,
    canonical_json_bytes,
    safe_archive_filename,
    sha256_file,
    validate_archive_path,
)

OWNER_NAMESPACE = UUID("7d54effd-bc4b-4c43-beb0-9a10ac0a078a")


class BackupExporter:
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

    def validate_source_path(self, source: Path) -> Path:
        return validate_archive_path(self.upload_root, source)

    def export(self, user: User, destination: Path) -> BackupExportResult:
        if user.id is None:
            raise ValueError("Backup exports require a persisted user")

        owner_id = uuid5(OWNER_NAMESPACE, f"cognolith-user:{user.id}")
        backup_id = uuid4()
        created_at = datetime.now(UTC)
        records, document_files = self._collect_records(user.id, owner_id)
        record_filenames = {name: f"{name}.json" for name in records}
        checksums: dict[str, str] = {}
        destination = Path(destination)
        temporary_destination = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        manifest: BackupManifestV1 | None = None

        try:
            with VersionedZipWriter(temporary_destination, self.maximum_archive_size) as archive:
                for name, value in records.items():
                    archive_name = record_filenames[name]
                    checksums[archive_name] = archive.write_bytes(archive_name, canonical_json_bytes(value))
                for document, source in document_files:
                    archive_name = (
                        f"files/documents/{document.portable_id}/{safe_archive_filename(document.file_name)}"
                    )
                    checksums[archive_name] = archive.write_file(archive_name, source)

                manifest = BackupManifestV1(
                    schema_version=1,
                    backup_id=backup_id,
                    owner_id=owner_id,
                    created_at=created_at,
                    app_version=self.app_version,
                    counts={name: len(value) for name, value in records.items()},
                    checksums=checksums,
                    record_filenames=record_filenames,
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

    def _collect_records(
        self, user_id: int, owner_id: UUID
    ) -> tuple[dict[str, list[dict[str, Any]]], list[tuple[Document, Path]]]:
        notes_by_id = _by_id(self.session.exec(select(Notes).where(Notes.user_id == user_id)).all())
        all_links = list(self.session.exec(select(NoteLinks)).all())
        included_note_ids = _include_related_notes(notes_by_id, all_links)
        notes = _selected(notes_by_id, included_note_ids)

        folders_by_id = _by_id(
            self.session.exec(select(NoteFolders).where(NoteFolders.user_id == user_id)).all()
        )
        included_folder_ids = {folder_id for folder_id, folder in folders_by_id.items() if not folder.is_deleted}
        included_folder_ids.update(note.folder_id for note in notes if note.folder_id is not None)
        _include_parent_folders(folders_by_id, included_folder_ids)
        folders = _selected(folders_by_id, included_folder_ids)

        documents_by_id = _by_id(
            self.session.exec(select(Document).where(Document.user_id == user_id)).all()
        )
        included_document_ids = {
            document_id for document_id, document in documents_by_id.items() if not document.is_deleted
        }
        included_document_ids.update(
            note.linked_document_id for note in notes if note.linked_document_id is not None
        )
        documents = _selected(documents_by_id, included_document_ids)

        sessions_by_id = _by_id(
            self.session.exec(select(ChatSession).where(ChatSession.user_id == user_id)).all()
        )
        included_session_ids = set(sessions_by_id)
        included_session_ids.update(
            note.linked_chat_session_id for note in notes if note.linked_chat_session_id is not None
        )
        sessions = _selected(sessions_by_id, included_session_ids)

        tags_by_id = _by_id(self.session.exec(select(NoteTags).where(NoteTags.user_id == user_id)).all())
        relations = [
            relation
            for relation in self.session.exec(select(NoteTagRelations)).all()
            if relation.note_id in included_note_ids and relation.tag_id in tags_by_id
        ]
        links = [
            link
            for link in all_links
            if link.source_note_id in included_note_ids and link.target_note_id in included_note_ids
        ]
        templates = list(
            self.session.exec(select(NoteTemplates).where(NoteTemplates.user_id == user_id)).all()
        )
        messages = [
            message
            for message in self.session.exec(select(ChatMessages)).all()
            if message.session_id in included_session_ids
        ]
        user_settings = self.session.get(UserSettings, user_id)

        records: dict[str, list[dict[str, Any]]] = {
            "notes": [_note_record(note, folders_by_id, documents_by_id, sessions_by_id, notes_by_id) for note in notes],
            "folders": [_folder_record(folder, folders_by_id) for folder in folders],
            "tags": [_tag_record(tag) for tag in tags_by_id.values()],
            "note_tag_relations": [_tag_relation_record(relation, notes_by_id, tags_by_id) for relation in relations],
            "links": [_link_record(link, notes_by_id) for link in links],
            "templates": [_template_record(template, owner_id) for template in templates],
            "documents": [_document_record(document) for document in documents],
            "chat_sessions": [_session_record(session) for session in sessions],
            "chat_messages": [_message_record(message, sessions_by_id) for message in messages],
            "user_preferences": [_preferences_record(user_settings, folders_by_id)] if user_settings else [],
        }
        for value in records.values():
            value.sort(key=canonical_json_bytes)
        document_files = [(document, self.validate_source_path(Path(document.file_path))) for document in documents]
        return records, document_files


def _by_id(models: Sequence[Any]) -> dict[int, Any]:
    return {model.id: model for model in models if model.id is not None}


def _selected(models_by_id: dict[int, Any], identifiers: Collection[int]) -> list[Any]:
    return [models_by_id[identifier] for identifier in identifiers if identifier in models_by_id]


def _include_related_notes(notes_by_id: dict[int, Notes], links: list[NoteLinks]) -> set[int]:
    included = {note_id for note_id, note in notes_by_id.items() if not note.is_deleted}
    changed = True
    while changed:
        changed = False
        for note_id in tuple(included):
            note = notes_by_id[note_id]
            for related_id in (note.parent_note_id, note.previous_version_id):
                if related_id in notes_by_id and related_id not in included:
                    included.add(related_id)
                    changed = True
        for link in links:
            if link.source_note_id in included and link.target_note_id in notes_by_id:
                if link.target_note_id not in included:
                    included.add(link.target_note_id)
                    changed = True
            if link.target_note_id in included and link.source_note_id in notes_by_id:
                if link.source_note_id not in included:
                    included.add(link.source_note_id)
                    changed = True
    return included


def _include_parent_folders(folders_by_id: dict[int, NoteFolders], included: set[int]) -> None:
    changed = True
    while changed:
        changed = False
        for folder_id in tuple(included):
            folder = folders_by_id.get(folder_id)
            parent_folder_id = folder.parent_folder_id if folder is not None else None
            if (
                parent_folder_id is not None
                and parent_folder_id in folders_by_id
                and parent_folder_id not in included
            ):
                included.add(parent_folder_id)
                changed = True


def _portable_id(model: Any) -> str:
    return str(model.portable_id)


def _portable_reference(models: dict[int, Any], identifier: int | None) -> str | None:
    model = models.get(identifier) if identifier is not None else None
    return _portable_id(model) if model is not None else None


def _record_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _base_record(model: Any) -> dict[str, Any]:
    return {"id": _portable_id(model), "created_at": _record_time(model.created_at)}


def _note_record(
    note: Notes,
    folders: dict[int, NoteFolders],
    documents: dict[int, Document],
    sessions: dict[int, ChatSession],
    notes: dict[int, Notes],
) -> dict[str, Any]:
    record = _base_record(note)
    record.update(
        {
            "updated_at": _record_time(note.updated_at),
            "folder_id": _portable_reference(folders, note.folder_id),
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
            "linked_document_id": _portable_reference(documents, note.linked_document_id),
            "linked_chat_session_id": _portable_reference(sessions, note.linked_chat_session_id),
            "parent_note_id": _portable_reference(notes, note.parent_note_id),
            "version": note.version,
            "previous_version_id": _portable_reference(notes, note.previous_version_id),
            "is_public": note.is_public,
            "is_deleted": note.is_deleted,
            "last_edited_at": _record_time(note.last_edited_at),
        }
    )
    return record


def _folder_record(folder: NoteFolders, folders: dict[int, NoteFolders]) -> dict[str, Any]:
    record = _base_record(folder)
    record.update(
        {
            "updated_at": _record_time(folder.updated_at),
            "name": folder.name,
            "description": folder.description,
            "parent_folder_id": _portable_reference(folders, folder.parent_folder_id),
            "color": folder.color,
            "icon": folder.icon,
            "emoji": folder.emoji,
            "is_shared": folder.is_shared,
            "is_archived": folder.is_archived,
            "sort_order": folder.sort_order,
            "is_deleted": folder.is_deleted,
        }
    )
    return record


def _tag_record(tag: NoteTags) -> dict[str, Any]:
    return {
        "id": _portable_id(tag),
        "name": tag.name,
        "color": tag.color,
        "description": tag.description,
        "created_at": _record_time(tag.created_at),
    }


def _tag_relation_record(
    relation: NoteTagRelations, notes: dict[int, Notes], tags: dict[int, NoteTags]
) -> dict[str, Any]:
    return {
        "note_id": _portable_id(notes[relation.note_id]),
        "tag_id": _portable_id(tags[relation.tag_id]),
        "created_at": _record_time(relation.created_at),
    }


def _link_record(link: NoteLinks, notes: dict[int, Notes]) -> dict[str, Any]:
    source_note_id = _portable_reference(notes, link.source_note_id)
    target_note_id = _portable_reference(notes, link.target_note_id)
    if source_note_id is None or target_note_id is None:
        raise ValueError("Backup link references a missing note")
    return {
        "id": _portable_id(link),
        "source_note_id": source_note_id,
        "target_note_id": target_note_id,
        "link_type": link.link_type.value,
        "description": link.description,
        "created_at": _record_time(link.created_at),
    }


def _template_record(template: NoteTemplates, owner_id: UUID) -> dict[str, Any]:
    return {
        "id": str(uuid5(owner_id, f"template:{template.id}")),
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


def _document_record(document: Document) -> dict[str, Any]:
    record = _base_record(document)
    record.update(
        {
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
    )
    return record


def _session_record(session: ChatSession) -> dict[str, Any]:
    record = _base_record(session)
    record.update(
        {
            "updated_at": _record_time(session.updated_at),
            "title": session.title,
            "description": session.description,
            "is_archived": session.is_archived,
            "is_pinned": session.is_pinned,
            "last_message_at": _record_time(session.last_message_at),
        }
    )
    return record


def _message_record(message: ChatMessages, sessions: dict[int, ChatSession]) -> dict[str, Any]:
    session_id = _portable_reference(sessions, message.session_id)
    if session_id is None:
        raise ValueError("Backup message references a missing chat session")
    record = _base_record(message)
    record.update(
        {
            "updated_at": _record_time(message.updated_at),
            "session_id": session_id,
            "role": message.role.value,
            "content": message.content,
        }
    )
    return record


def _preferences_record(
    preferences: UserSettings, folders: dict[int, NoteFolders]
) -> dict[str, Any]:
    return {
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
        "default_note_folder_id": _portable_reference(folders, preferences.default_note_folder_id),
        "email_notifications": preferences.email_notifications,
        "processing_notifications": preferences.processing_notifications,
        "rag_diagnostics_enabled": preferences.rag_diagnostics_enabled,
        "created_at": _record_time(preferences.created_at),
        "updated_at": _record_time(preferences.updated_at),
    }


def _manifest_record(manifest: BackupManifestV1) -> dict[str, Any]:
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


__all__ = ["BackupExporter", "validate_archive_path"]
