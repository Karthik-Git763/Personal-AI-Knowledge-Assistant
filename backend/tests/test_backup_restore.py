from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import event
from sqlmodel import Session, col, select

from app.core.database import engine
from app.models.backup import (
    BackupOperationKind,
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    GoogleDriveConnection,
    WorkspaceBackup,
)
from app.models.chat import ChatMessages, ChatRole, ChatSession
from app.models.document import Document, DocumentChunks
from app.models.note import (
    NoteCategory,
    NoteFolders,
    NoteLinks,
    Notes,
    NoteTagRelations,
    NoteTags,
    NoteTemplates,
)
from app.models.user import User, UserSettings
from app.schemas.backup import BackupPreview, RestoreResult
from app.services.backup_archive import BackupExportResult, BackupManifestV1
from app.services.backup_coordinator import BackupCoordinator, BackupPreconditionError
from app.services.backup_exporter import RECORD_FILENAMES
from app.services.backup_importer import BackupImporter, BackupRestoreFailed
from app.services.backup_store import BackupObjectMetadata, StoredBackup
from app.services.google_drive_oauth import derive_drive_owner_id


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _timestamp() -> str:
    return "2026-07-24T12:00:00+00:00"


def _restore_records() -> tuple[dict[str, list[dict[str, Any]]], dict[str, UUID]]:
    identifiers = {
        name: uuid4()
        for name in (
            "folder",
            "child_folder",
            "tag",
            "document",
            "chat",
            "message",
            "parent_note",
            "note",
            "link",
            "template",
        )
    }
    records: dict[str, list[dict[str, Any]]] = {
        "folders": [
            {
                "id": str(identifiers["folder"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "name": "Restored",
                "description": "root",
                "parent_folder_id": None,
                "color": "#123456",
                "icon": "folder",
                "emoji": None,
                "is_shared": False,
                "is_archived": False,
                "sort_order": 1,
                "is_deleted": False,
            },
            {
                "id": str(identifiers["child_folder"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "name": "Child",
                "description": None,
                "parent_folder_id": str(identifiers["folder"]),
                "color": None,
                "icon": None,
                "emoji": None,
                "is_shared": False,
                "is_archived": False,
                "sort_order": 2,
                "is_deleted": False,
            },
        ],
        "tags": [
            {
                "id": str(identifiers["tag"]),
                "name": "restored-tag",
                "color": None,
                "description": None,
                "created_at": _timestamp(),
            }
        ],
        "documents": [
            {
                "id": str(identifiers["document"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "title": "Restored document",
                "file_name": "restored.txt",
                "file_size": len(b"restored document"),
                "file_type": "txt",
                "mime_type": "text/plain",
                "tags": ["restored"],
                "language": "en",
                "is_deleted": False,
            }
        ],
        "chat_sessions": [
            {
                "id": str(identifiers["chat"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "title": "Restored chat",
                "description": None,
                "is_archived": False,
                "is_pinned": True,
                "last_message_at": _timestamp(),
            }
        ],
        "chat_messages": [
            {
                "id": str(identifiers["message"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "session_id": str(identifiers["chat"]),
                "role": "assistant",
                "content": "restored response",
            }
        ],
        "notes": [
            {
                "id": str(identifiers["parent_note"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "folder_id": str(identifiers["folder"]),
                "title": "Parent",
                "content": "parent content",
                "content_type": "markdown",
                "keywords": ["parent"],
                "ai_generated": False,
                "is_favorite": False,
                "is_archived": False,
                "is_pinned": False,
                "color": None,
                "emoji": None,
                "linked_document_id": None,
                "linked_chat_session_id": None,
                "parent_note_id": None,
                "version": 1,
                "previous_version_id": None,
                "is_public": False,
                "is_deleted": False,
                "last_edited_at": _timestamp(),
            },
            {
                "id": str(identifiers["note"]),
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
                "folder_id": str(identifiers["child_folder"]),
                "title": "Restored note",
                "content": "restored content",
                "content_type": "markdown",
                "keywords": ["restored"],
                "ai_generated": True,
                "is_favorite": True,
                "is_archived": False,
                "is_pinned": True,
                "color": "#abcdef",
                "emoji": None,
                "linked_document_id": str(identifiers["document"]),
                "linked_chat_session_id": str(identifiers["chat"]),
                "parent_note_id": str(identifiers["parent_note"]),
                "version": 2,
                "previous_version_id": str(identifiers["parent_note"]),
                "is_public": False,
                "is_deleted": False,
                "last_edited_at": _timestamp(),
            },
        ],
        "note_tag_relations": [
            {
                "note_id": str(identifiers["note"]),
                "tag_id": str(identifiers["tag"]),
                "created_at": _timestamp(),
            }
        ],
        "links": [
            {
                "id": str(identifiers["link"]),
                "source_note_id": str(identifiers["parent_note"]),
                "target_note_id": str(identifiers["note"]),
                "link_type": "related",
                "description": "restored link",
                "created_at": _timestamp(),
            }
        ],
        "templates": [
            {
                "id": str(identifiers["template"]),
                "name": "Restored template",
                "description": None,
                "category": "work",
                "content": "# Template",
                "content_type": "markdown",
                "is_public": False,
                "is_system": False,
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            }
        ],
        "user_preferences": [
            {
                "llm_provider": "openai",
                "llm_model": "gpt-test",
                "embedding_model": "embedding-test",
                "chunk_size": 800,
                "chunk_overlap": 100,
                "top_k_results": 7,
                "similarity_threshold": 0.75,
                "temperature": 0.25,
                "max_tokens": 1200,
                "theme": "dark",
                "language": "en",
                "notes_view_mode": "list",
                "default_note_folder_id": str(identifiers["child_folder"]),
                "email_notifications": False,
                "processing_notifications": False,
                "rag_diagnostics_enabled": True,
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            }
        ],
    }
    return records, identifiers


def _write_restore_archive(
    path: Path, owner_id: UUID, records: dict[str, list[dict[str, Any]]]
) -> Path:
    document = records["documents"][0]
    document_name = (
        f"files/documents/{document['id']}/{document['file_name']}"
    )
    payloads = {
        RECORD_FILENAMES[name]: _json_bytes(value) for name, value in records.items()
    }
    payloads[document_name] = b"restored document"
    manifest = {
        "schema_version": 1,
        "backup_id": str(uuid4()),
        "owner_id": str(owner_id),
        "created_at": _timestamp(),
        "app_version": "0.1.0",
        "counts": {name: len(value) for name, value in records.items()},
        "checksums": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
        },
        "record_filenames": RECORD_FILENAMES,
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", _json_bytes(manifest))
    return path


@pytest.fixture
def restore_workspace(
    session: Session, tmp_path: Path
) -> dict[str, Any]:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    old_file = upload_root / "old.txt"
    old_file.write_bytes(b"old document")
    other_file = upload_root / "other.txt"
    other_file.write_bytes(b"other document")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"outside document")

    user = User(email="restore@example.com", hashed_password="hashed")
    other_user = User(email="other-restore@example.com", hashed_password="hashed")
    session.add_all([user, other_user])
    session.commit()
    session.refresh(user)
    session.refresh(other_user)
    assert user.id is not None
    assert other_user.id is not None

    folder = NoteFolders(user_id=user.id, name="Old folder")
    tag = NoteTags(user_id=user.id, name="old-tag")
    chat = ChatSession(user_id=user.id, title="Old chat")
    old_document = Document(
        user_id=user.id,
        title="Old document",
        file_name=old_file.name,
        file_path=str(old_file),
        file_size=old_file.stat().st_size,
        file_type="txt",
        mime_type="text/plain",
        content="derived content",
        summary="derived summary",
        content_preview="derived preview",
        status="completed",
    )
    outside_document = Document(
        user_id=user.id,
        title="Outside document",
        file_name=outside_file.name,
        file_path=str(outside_file),
        file_size=outside_file.stat().st_size,
        file_type="txt",
        mime_type="text/plain",
    )
    other_document = Document(
        user_id=other_user.id,
        title="Other document",
        file_name=other_file.name,
        file_path=str(other_file),
        file_size=other_file.stat().st_size,
        file_type="txt",
        mime_type="text/plain",
    )
    session.add_all([folder, tag, chat, old_document, outside_document, other_document])
    session.flush()
    note = Notes(
        user_id=user.id,
        folder_id=folder.id,
        title="Old note",
        content="old content",
        summary="old summary",
        content_preview="old preview",
        linked_document_id=old_document.id,
        linked_chat_session_id=chat.id,
    )
    other_note = Notes(user_id=other_user.id, title="Other note", content="other content")
    session.add_all([note, other_note])
    session.flush()
    session.add(NoteTagRelations(note_id=note.id, tag_id=tag.id))  # type: ignore[arg-type]
    session.add(
        DocumentChunks(
            document_id=old_document.id,
            chunk_index=0,
            content="old chunk",
            vector_id="old-vector",
        )
    )
    session.add(
        ChatMessages(
            session_id=chat.id,
            role=ChatRole.user,
            content="old message",
        )
    )
    session.add(
        NoteTemplates(
            user_id=user.id,
            name="Old template",
            content="old",
            category=NoteCategory.work,
        )
    )
    session.add(UserSettings(user_id=user.id, llm_model="old-model"))
    session.commit()

    records, identifiers = _restore_records()
    archive_owner_id = uuid4()
    archive = _write_restore_archive(
        tmp_path / "restore.zip", archive_owner_id, records
    )
    with ZipFile(archive) as source:
        archive_backup_id = UUID(
            json.loads(source.read("manifest.json"))["backup_id"]
        )
    return {
        "user": user,
        "other_user": other_user,
        "archive": archive,
        "archive_owner_id": archive_owner_id,
        "archive_backup_id": archive_backup_id,
        "identifiers": identifiers,
        "upload_root": upload_root,
        "temp_root": tmp_path / "backup-temp",
        "old_file": old_file,
        "other_file": other_file,
        "outside_file": outside_file,
        "other_note_id": other_note.id,
        "other_document_id": other_document.id,
    }


def _importer(
    session: Session,
    workspace: dict[str, Any],
    *,
    move_file: Any = None,
) -> BackupImporter:
    return BackupImporter(
        session=session,
        upload_root=workspace["upload_root"],
        temporary_directory=workspace["temp_root"],
        supported_app_versions={"0.1.0"},
        move_file=move_file,
    )


def _pending_restore_operation(
    session: Session, user_id: int
) -> WorkspaceBackup:
    operation = WorkspaceBackup(
        user_id=user_id,
        operation_kind=BackupOperationKind.restore,
        source_backup_id=uuid4(),
        status=BackupStatus.pending,
        trigger=BackupTrigger.manual,
        started_at=datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
    )
    session.add(operation)
    session.commit()
    session.refresh(operation)
    return operation


def _snapshot_workspace(session: Session, user_id: int) -> tuple[Any, ...]:
    notes = session.exec(
        select(Notes).where(Notes.user_id == user_id).order_by(col(Notes.portable_id))
    ).all()
    documents = session.exec(
        select(Document).where(Document.user_id == user_id).order_by(col(Document.portable_id))
    ).all()
    settings = session.get(UserSettings, user_id)
    return (
        [(note.portable_id, note.title, note.content, note.folder_id) for note in notes],
        [
            (
                document.portable_id,
                document.title,
                document.file_path,
                document.content,
                document.status,
            )
            for document in documents
        ],
        settings.llm_model if settings else None,
    )


def test_restore_replaces_workspace_and_remaps_relationships(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    original_user_portable_id = user.portable_id
    importer = _importer(session, restore_workspace)
    operation = _pending_restore_operation(session, user.id)

    result = importer.restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    session.refresh(user)
    assert user.portable_id == original_user_portable_id
    restored_note = session.exec(
        select(Notes).where(
            Notes.user_id == user.id,
            Notes.portable_id == restore_workspace["identifiers"]["note"],
        )
    ).one()
    parent_note = session.exec(
        select(Notes).where(
            Notes.user_id == user.id,
            Notes.portable_id == restore_workspace["identifiers"]["parent_note"],
        )
    ).one()
    restored_document = session.exec(
        select(Document).where(
            Document.user_id == user.id,
            Document.portable_id == restore_workspace["identifiers"]["document"],
        )
    ).one()
    restored_chat = session.exec(
        select(ChatSession).where(
            ChatSession.user_id == user.id,
            ChatSession.portable_id == restore_workspace["identifiers"]["chat"],
        )
    ).one()
    restored_folder = session.exec(
        select(NoteFolders).where(
            NoteFolders.user_id == user.id,
            NoteFolders.portable_id == restore_workspace["identifiers"]["child_folder"],
        )
    ).one()
    restored_tag = session.exec(
        select(NoteTags).where(
            NoteTags.user_id == user.id,
            NoteTags.portable_id == restore_workspace["identifiers"]["tag"],
        )
    ).one()
    relation = session.exec(
        select(NoteTagRelations).where(NoteTagRelations.note_id == restored_note.id)
    ).one()
    restored_link = session.exec(
        select(NoteLinks).where(
            NoteLinks.portable_id == restore_workspace["identifiers"]["link"]
        )
    ).one()
    settings = session.get(UserSettings, user.id)

    assert restored_note.parent_note_id == parent_note.id
    assert restored_note.previous_version_id == parent_note.id
    assert restored_note.folder_id == restored_folder.id
    assert restored_note.linked_document_id == restored_document.id
    assert restored_note.linked_chat_session_id == restored_chat.id
    assert relation.tag_id == restored_tag.id
    assert restored_link.source_note_id == parent_note.id
    assert restored_link.target_note_id == restored_note.id
    assert restored_note.summary is None
    assert restored_note.content_preview is None
    assert restored_note.word_count is None
    assert restored_document.content is None
    assert restored_document.summary is None
    assert restored_document.content_preview is None
    assert restored_document.chunk_count == 0
    assert restored_document.status == "processing"
    assert session.exec(
        select(DocumentChunks).where(DocumentChunks.document_id == restored_document.id)
    ).all() == []
    assert Path(restored_document.file_path).read_bytes() == b"restored document"
    assert result.reprocessing_document_ids == [restored_document.id]
    assert settings is not None
    assert settings.llm_model == "gpt-test"
    assert settings.default_note_folder_id == restored_folder.id
    assert session.get(Notes, restore_workspace["other_note_id"]) is not None
    assert session.get(Document, restore_workspace["other_document_id"]) is not None
    assert restore_workspace["other_file"].read_bytes() == b"other document"
    assert not restore_workspace["old_file"].exists()
    assert restore_workspace["outside_file"].read_bytes() == b"outside document"
    assert not list(restore_workspace["temp_root"].glob("restore-*"))


def test_restore_success_cannot_leave_operation_stale_pending(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    assert operation.id is not None
    completed_at = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)
    importer = _importer(session, restore_workspace)

    importer.restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=completed_at,
    )

    with Session(engine) as verification_session:
        persisted = verification_session.get(WorkspaceBackup, operation.id)
        assert persisted is not None
        assert persisted.status == BackupStatus.completed
        assert persisted.completed_at == completed_at
        assert persisted.failure_message is None
        assert persisted.schema_version == 1
        assert persisted.archive_size_bytes == restore_workspace["archive"].stat().st_size
        assert persisted.item_counts == {
            "folders": 2,
            "tags": 1,
            "documents": 1,
            "chat_sessions": 1,
            "chat_messages": 1,
            "notes": 2,
            "note_tag_relations": 1,
            "links": 1,
            "templates": 1,
            "user_preferences": 1,
        }
        restored_note = verification_session.exec(
            select(Notes).where(
                Notes.user_id == user.id,
                Notes.portable_id
                == restore_workspace["identifiers"]["note"],
            )
        ).one()
        assert restored_note.title == "Restored note"


def test_post_commit_journal_cleanup_failure_keeps_completed_restore(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    original_unlink = Path.unlink

    def fail_journal_unlink(
        path: Path, *args: Any, **kwargs: Any
    ) -> None:
        if path.name == "recovery-journal.json":
            raise OSError("injected post-commit journal cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink)
    importer = _importer(session, restore_workspace)

    result = importer.restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    session.refresh(operation)
    assert operation.status == BackupStatus.completed
    restored = session.get(Document, result.reprocessing_document_ids[0])
    assert restored is not None
    assert Path(restored.file_path).read_bytes() == b"restored document"


def test_post_commit_old_file_query_failure_is_logged_and_restore_stays_completed(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    original_exec = session.exec
    restore_committed = False

    def mark_restore_committed(database_session: Session) -> None:
        nonlocal restore_committed
        restore_committed = True

    def fail_cleanup_query(*args: Any, **kwargs: Any) -> Any:
        if restore_committed:
            raise RuntimeError("sensitive injected query detail")
        return original_exec(*args, **kwargs)

    event.listen(session, "after_commit", mark_restore_committed, once=True)
    monkeypatch.setattr(session, "exec", fail_cleanup_query)

    with caplog.at_level(
        "WARNING", logger="app.services.backup_importer"
    ):
        result = _importer(session, restore_workspace).restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert len(result.reprocessing_document_ids) == 1
    with Session(engine) as verification_session:
        persisted = verification_session.get(WorkspaceBackup, operation.id)
        assert persisted is not None
        assert persisted.status == BackupStatus.completed
    assert restore_workspace["old_file"].exists()
    record = next(
        record
        for record in caplog.records
        if record.name == "app.services.backup_importer"
    )
    assert record.getMessage() == "Post-restore old-file cleanup failed"
    assert record.__dict__["cleanup_error_type"] == "RuntimeError"
    assert "sensitive injected query detail" not in caplog.text


def test_post_commit_old_file_root_failure_is_logged_and_restore_stays_completed(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    original_resolve = Path.resolve
    restore_committed = False

    def mark_restore_committed(database_session: Session) -> None:
        nonlocal restore_committed
        restore_committed = True

    def fail_upload_root_resolve(
        path: Path, *args: Any, **kwargs: Any
    ) -> Path:
        if restore_committed and path == restore_workspace["upload_root"]:
            raise OSError(errno.EIO, "sensitive injected root detail")
        return original_resolve(path, *args, **kwargs)

    event.listen(session, "after_commit", mark_restore_committed, once=True)
    monkeypatch.setattr(Path, "resolve", fail_upload_root_resolve)

    with caplog.at_level(
        "WARNING", logger="app.services.backup_importer"
    ):
        result = _importer(session, restore_workspace).restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert len(result.reprocessing_document_ids) == 1
    with Session(engine) as verification_session:
        persisted = verification_session.get(WorkspaceBackup, operation.id)
        assert persisted is not None
        assert persisted.status == BackupStatus.completed
    assert restore_workspace["old_file"].exists()
    record = next(
        record
        for record in caplog.records
        if record.name == "app.services.backup_importer"
    )
    assert record.getMessage() == "Post-restore old-file cleanup failed"
    assert record.__dict__["cleanup_error_type"] == "OSError"
    assert "sensitive injected root detail" not in caplog.text


def test_post_commit_old_file_unlink_failure_is_logged_and_restore_stays_completed(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    old_file: Path = restore_workspace["old_file"]
    original_unlink = Path.unlink

    def fail_old_file_unlink(
        path: Path, *args: Any, **kwargs: Any
    ) -> None:
        if path == old_file:
            raise OSError(errno.EACCES, "sensitive injected unlink detail")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_file_unlink)

    with caplog.at_level(
        "WARNING", logger="app.services.backup_importer"
    ):
        result = _importer(session, restore_workspace).restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert len(result.reprocessing_document_ids) == 1
    session.refresh(operation)
    assert operation.status == BackupStatus.completed
    assert old_file.exists()
    record = next(
        record
        for record in caplog.records
        if record.name == "app.services.backup_importer"
    )
    assert record.getMessage() == "Post-restore old-file cleanup failed"
    assert record.__dict__["cleanup_error_type"] == "PermissionError"
    assert "sensitive injected unlink detail" not in caplog.text


def test_restore_fsyncs_journal_and_final_file_before_database_commit(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    events: list[str] = []
    original_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        events.append("directory-fsync" if stat.S_ISDIR(mode) else "file-fsync")
        original_fsync(descriptor)

    def track_commit(database_session: Session) -> None:
        events.append("commit")

    monkeypatch.setattr("app.services.backup_importer.os.fsync", track_fsync)
    event.listen(session, "before_commit", track_commit, once=True)

    _importer(session, restore_workspace).restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    commit_index = events.index("commit")
    before_commit = events[:commit_index]
    assert before_commit.count("file-fsync") >= 3
    assert before_commit.count("directory-fsync") >= 2


def test_final_directory_fsync_failure_rolls_back_and_removes_new_file(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)
    operation = _pending_restore_operation(session, user.id)
    original_fsync = os.fsync
    directory_syncs = 0

    def fail_second_directory_fsync(descriptor: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(
        "app.services.backup_importer.os.fsync",
        fail_second_directory_fsync,
    )

    with pytest.raises(BackupRestoreFailed):
        _importer(session, restore_workspace).restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    session.refresh(operation)
    assert directory_syncs == 2
    assert operation.status == BackupStatus.pending
    assert _snapshot_workspace(session, user.id) == before
    assert sorted(path.name for path in restore_workspace["upload_root"].iterdir()) == [
        "old.txt",
        "other.txt",
    ]
    assert not list(restore_workspace["temp_root"].glob("restore-*"))


def test_unsupported_directory_fsync_does_not_fail_restore(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    operation = _pending_restore_operation(session, user.id)
    original_fsync = os.fsync
    rejected_directory_syncs = 0

    def reject_directory_fsync(descriptor: int) -> None:
        nonlocal rejected_directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            rejected_directory_syncs += 1
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        original_fsync(descriptor)

    monkeypatch.setattr(
        "app.services.backup_importer.os.fsync",
        reject_directory_fsync,
    )

    result = _importer(session, restore_workspace).restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    session.refresh(operation)
    assert rejected_directory_syncs == 2
    assert operation.status == BackupStatus.completed
    assert len(result.reprocessing_document_ids) == 1


def test_restore_failure_keeps_workspace_and_files_unchanged(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)  # type: ignore[arg-type]

    class FailingImporter(BackupImporter):
        def _insert_note_relations(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected database failure")

    importer = FailingImporter(
        session=session,
        upload_root=restore_workspace["upload_root"],
        temporary_directory=restore_workspace["temp_root"],
        supported_app_versions={"0.1.0"},
    )
    operation = _pending_restore_operation(session, user.id)

    with pytest.raises(BackupRestoreFailed):
        importer.restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert _snapshot_workspace(session, user.id) == before  # type: ignore[arg-type]
    assert restore_workspace["old_file"].read_bytes() == b"old document"
    assert restore_workspace["outside_file"].read_bytes() == b"outside document"
    assert sorted(path.name for path in restore_workspace["upload_root"].iterdir()) == [
        "old.txt",
        "other.txt",
    ]
    assert not list(restore_workspace["temp_root"].glob("restore-*"))


def test_restore_move_failure_rolls_back_without_new_files(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)  # type: ignore[arg-type]

    def fail_move(source: Path, destination: Path) -> None:
        raise OSError("injected move failure")

    importer = _importer(session, restore_workspace, move_file=fail_move)
    operation = _pending_restore_operation(session, user.id)

    with pytest.raises(BackupRestoreFailed):
        importer.restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert _snapshot_workspace(session, user.id) == before  # type: ignore[arg-type]
    assert sorted(path.name for path in restore_workspace["upload_root"].iterdir()) == [
        "old.txt",
        "other.txt",
    ]


def test_restore_commit_failure_removes_moved_files_and_rolls_back(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)  # type: ignore[arg-type]
    operation = _pending_restore_operation(session, user.id)

    def fail_commit(database_session: Session) -> None:
        raise RuntimeError("injected commit failure")

    event.listen(session, "before_commit", fail_commit, once=True)
    importer = _importer(session, restore_workspace)
    with pytest.raises(BackupRestoreFailed):
        importer.restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert _snapshot_workspace(session, user.id) == before  # type: ignore[arg-type]
    assert sorted(path.name for path in restore_workspace["upload_root"].iterdir()) == [
        "old.txt",
        "other.txt",
    ]


def test_restore_rechecks_document_checksum_while_staging(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)  # type: ignore[arg-type]

    class MutatingImporter(BackupImporter):
        def _validate(
            self,
            path: Path,
            expected_workspace_owner_id: UUID,
            expected_archive_backup_id: UUID | None = None,
        ) -> Any:
            validated = super()._validate(
                path,
                expected_workspace_owner_id,
                expected_archive_backup_id,
            )
            with ZipFile(path) as source:
                payloads = {
                    info.filename: source.read(info)
                    for info in source.infolist()
                }
            document_entry = next(
                name for name in payloads if name.startswith("files/documents/")
            )
            payloads[document_entry] = b"tampered document"
            with ZipFile(path, "w", ZIP_DEFLATED) as output:
                for name, payload in payloads.items():
                    output.writestr(name, payload)
            return validated

    importer = MutatingImporter(
        session=session,
        upload_root=restore_workspace["upload_root"],
        temporary_directory=restore_workspace["temp_root"],
        supported_app_versions={"0.1.0"},
    )
    operation = _pending_restore_operation(session, user.id)

    with pytest.raises(BackupRestoreFailed):
        importer.restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert _snapshot_workspace(session, user.id) == before  # type: ignore[arg-type]
    assert restore_workspace["old_file"].read_bytes() == b"old document"


def test_restore_falls_back_to_copy_for_cross_device_move(
    session: Session,
    restore_workspace: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None

    def cross_device(
        source: Path, destination: Path, **kwargs: Any
    ) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr("app.services.backup_importer.os.replace", cross_device)
    importer = _importer(session, restore_workspace)
    operation = _pending_restore_operation(session, user.id)

    result = importer.restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    assert len(result.reprocessing_document_ids) == 1
    restored = session.get(Document, result.reprocessing_document_ids[0])
    assert restored is not None
    assert Path(restored.file_path).read_bytes() == b"restored document"


def test_restore_rejects_symlinked_user_destination_parent(
    session: Session,
    restore_workspace: dict[str, Any],
    tmp_path: Path,
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)
    outside = tmp_path / "outside-upload"
    outside.mkdir()
    user_directory = restore_workspace["upload_root"] / str(user.id)
    user_directory.symlink_to(outside, target_is_directory=True)
    operation = _pending_restore_operation(session, user.id)
    importer = _importer(session, restore_workspace)

    with pytest.raises(BackupRestoreFailed):
        importer.restore(
            restore_workspace["archive"],
            user,
            expected_workspace_owner_id=restore_workspace["archive_owner_id"],
            operation=operation,
            completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
        )

    assert list(outside.iterdir()) == []
    assert _snapshot_workspace(session, user.id) == before


def test_restore_keeps_old_file_referenced_by_equivalent_surviving_path(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    other_document = session.get(
        Document, restore_workspace["other_document_id"]
    )
    assert other_document is not None
    other_document.file_path = str(
        restore_workspace["upload_root"] / "nested" / ".." / "old.txt"
    )
    session.add(other_document)
    session.commit()
    user: User = restore_workspace["user"]
    assert user.id is not None
    importer = _importer(session, restore_workspace)
    operation = _pending_restore_operation(session, user.id)

    importer.restore(
        restore_workspace["archive"],
        user,
        expected_workspace_owner_id=restore_workspace["archive_owner_id"],
        operation=operation,
        completed_at=datetime(2026, 7, 24, 14, 0, tzinfo=UTC),
    )

    assert restore_workspace["old_file"].read_bytes() == b"old document"


def test_concurrent_user_restore_cannot_recover_live_journal(
    session: Session, restore_workspace: dict[str, Any], tmp_path: Path
) -> None:
    first_user: User = restore_workspace["user"]
    second_user: User = restore_workspace["other_user"]
    assert first_user.id is not None
    assert second_user.id is not None
    second_records, _ = _restore_records()
    second_owner_id = uuid4()
    second_archive = _write_restore_archive(
        tmp_path / "second-restore.zip",
        second_owner_id,
        second_records,
    )
    first_operation = _pending_restore_operation(session, first_user.id)
    second_operation = _pending_restore_operation(session, second_user.id)
    assert first_operation.id is not None
    assert second_operation.id is not None
    first_moved = Event()
    release_first = Event()
    second_recovery_entered = Event()

    class PausingImporter(BackupImporter):
        def _move_staged_documents(
            self,
            staged: Mapping[UUID, Path],
            planned: Mapping[UUID, Path],
            created_paths: list[Path],
        ) -> None:
            super()._move_staged_documents(staged, planned, created_paths)
            first_moved.set()
            if not release_first.wait(timeout=10):
                raise RuntimeError("timed out waiting to release first restore")

    class ObservingImporter(BackupImporter):
        def _recover_incomplete_restores(self) -> None:
            second_recovery_entered.set()
            super()._recover_incomplete_restores()

    def run_first_restore() -> RestoreResult:
        with Session(engine) as first_session:
            user = first_session.get(User, first_user.id)
            operation = first_session.get(
                WorkspaceBackup, first_operation.id
            )
            assert user is not None
            assert operation is not None
            return PausingImporter(
                session=first_session,
                upload_root=restore_workspace["upload_root"],
                temporary_directory=restore_workspace["temp_root"],
                supported_app_versions={"0.1.0"},
            ).restore(
                restore_workspace["archive"],
                user,
                expected_workspace_owner_id=restore_workspace["archive_owner_id"],
                operation=operation,
                completed_at=datetime(
                    2026, 7, 24, 14, 0, tzinfo=UTC
                ),
            )

    def run_second_restore() -> RestoreResult:
        with Session(engine) as second_session:
            user = second_session.get(User, second_user.id)
            operation = second_session.get(
                WorkspaceBackup, second_operation.id
            )
            assert user is not None
            assert operation is not None
            return ObservingImporter(
                session=second_session,
                upload_root=restore_workspace["upload_root"],
                temporary_directory=restore_workspace["temp_root"],
                supported_app_versions={"0.1.0"},
            ).restore(
                second_archive,
                user,
                expected_workspace_owner_id=second_owner_id,
                operation=operation,
                completed_at=datetime(
                    2026, 7, 24, 14, 0, tzinfo=UTC
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run_first_restore)
        assert first_moved.wait(timeout=10)
        journal = next(
            restore_workspace["temp_root"].glob("restore-*/recovery-journal.json")
        )
        created_path = Path(
            json.loads(journal.read_text(encoding="utf-8"))["created_paths"][0]
        )
        second = executor.submit(run_second_restore)
        try:
            assert not second_recovery_entered.wait(timeout=1)
            assert journal.exists()
            assert created_path.exists()
        finally:
            release_first.set()
        first.result(timeout=10)
        with pytest.raises(BackupRestoreFailed):
            second.result(timeout=10)


class CoordinatorImporter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.preview_owner_ids: list[UUID] = []
        self.restore_owner_ids: list[UUID] = []
        self.preview_backup_ids: list[UUID | None] = []
        self.restore_backup_ids: list[UUID | None] = []
        self.fail_restore = False
        self.reject_commit_after_restore = False

    def preview(
        self,
        path: Path,
        expected_workspace_owner_id: UUID,
        expected_archive_backup_id: UUID | None = None,
    ) -> BackupPreview:
        self.preview_owner_ids.append(expected_workspace_owner_id)
        self.preview_backup_ids.append(expected_archive_backup_id)
        return BackupPreview(
            created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
            schema_version=1,
            app_version="0.1.0",
            archive_size_bytes=path.stat().st_size,
            item_counts={"notes": 1},
            warnings=[],
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
        self.restore_owner_ids.append(expected_workspace_owner_id)
        self.restore_backup_ids.append(expected_archive_backup_id)
        if self.fail_restore:
            raise BackupRestoreFailed("injected restore failure")
        operation.status = BackupStatus.completed
        operation.completed_at = completed_at
        operation.failure_message = None
        operation.schema_version = 1
        operation.archive_size_bytes = path.stat().st_size
        operation.item_counts = {"notes": 1}
        self.session.add(operation)
        self.session.commit()
        if self.reject_commit_after_restore:
            def reject_commit(database_session: Session) -> None:
                raise RuntimeError("coordinator attempted a second success commit")

            event.listen(self.session, "before_commit", reject_commit, once=True)
        return RestoreResult(reprocessing_document_ids=[101])


class CoordinatorExporter:
    def __init__(self, now: datetime, *, fail: bool = False) -> None:
        self.now = now
        self.fail = fail

    def export(
        self, user: User, destination: Path, *, backup_id: UUID | None = None
    ) -> BackupExportResult:
        if self.fail:
            raise RuntimeError("injected safety backup failure")
        destination.write_bytes(b"cognolith safety backup archive")
        manifest = BackupManifestV1(
            schema_version=1,
            backup_id=backup_id or uuid4(),
            owner_id=user.portable_id,
            created_at=self.now,
            app_version="0.1.0",
            counts={name: 0 for name in RECORD_FILENAMES},
            checksums={},
            record_filenames=RECORD_FILENAMES,
        )
        return BackupExportResult(
            path=destination,
            manifest=manifest,
            archive_checksum=hashlib.sha256(
                b"cognolith safety backup archive"
            ).hexdigest(),
            archive_size=len(b"cognolith safety backup archive"),
        )


class CoordinatorStore:
    def __init__(self, source: StoredBackup, archive: Path) -> None:
        self.backups = [source]
        self.archive = archive
        self.listed_owner_ids: list[UUID] = []
        self.downloaded_ids: list[str] = []

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]:
        self.listed_owner_ids.append(drive_owner_id)
        return list(self.backups)

    async def download(self, remote_id: str, destination: Path) -> Path:
        self.downloaded_ids.append(remote_id)
        destination.write_bytes(self.archive.read_bytes())
        return destination

    async def upload(
        self, archive: Path, metadata: BackupObjectMetadata
    ) -> StoredBackup:
        stored = StoredBackup(
            remote_id=f"safety-{metadata.backup_id}",
            name=archive.name,
            size=archive.stat().st_size,
            created_at=metadata.created_at,
            metadata=metadata,
            completed=True,
        )
        self.backups.append(stored)
        return stored

    async def delete(self, remote_id: str) -> None:
        raise AssertionError("Restore safety backup must not run retention")


class OAuthRefreshingCoordinatorStore(CoordinatorStore):
    def __init__(
        self,
        source: StoredBackup,
        archive: Path,
        session: Session,
        connection: GoogleDriveConnection,
        refreshed_at: datetime,
    ) -> None:
        super().__init__(source, archive)
        self.session = session
        self.connection = connection
        self.refreshed_at = refreshed_at

    async def list(self, drive_owner_id: UUID) -> list[StoredBackup]:
        self.connection.token_expires_at = self.refreshed_at
        self.session.add(self.connection)
        self.session.commit()
        return await super().list(drive_owner_id)


@pytest.fixture
def coordinator_restore(
    session: Session, tmp_path: Path
) -> Generator[dict[str, Any], None, None]:
    now = datetime(2026, 7, 24, 13, 0, tzinfo=UTC)
    user = User(email="coordinator-restore@example.com", hashed_password="hashed")
    other_user = User(email="other-coordinator@example.com", hashed_password="hashed")
    session.add_all([user, other_user])
    session.commit()
    session.refresh(user)
    session.refresh(other_user)
    assert user.id is not None
    assert other_user.id is not None
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=b"encrypted",
        google_subject="restore-google-subject",
        google_email=user.email,
        granted_scopes=["https://www.googleapis.com/auth/drive.appdata"],
    )
    schedule = BackupSchedule(user_id=user.id, enabled=True)
    archive = tmp_path / "remote.zip"
    archive.write_bytes(b"cognolith remote backup archive")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = WorkspaceBackup(
        user_id=user.id,
        status=BackupStatus.completed,
        trigger=BackupTrigger.manual,
        remote_file_id="source-remote",
        archive_size_bytes=archive.stat().st_size,
        checksum=checksum,
        schema_version=1,
        item_counts={"notes": 1},
        completed_at=now,
    )
    other_source = WorkspaceBackup(
        user_id=other_user.id,
        status=BackupStatus.completed,
        remote_file_id="other-source",
        checksum=checksum,
    )
    session.add_all([connection, schedule, source, other_source])
    session.commit()
    assert source.backup_id is not None
    archive_owner_id = uuid4()
    drive_owner_id = derive_drive_owner_id(connection.google_subject)
    remote = StoredBackup(
        remote_id="source-remote",
        name="remote.zip",
        size=archive.stat().st_size,
        created_at=now,
        metadata=BackupObjectMetadata(
            drive_owner_id=drive_owner_id,
            workspace_owner_id=archive_owner_id,
            backup_id=source.backup_id,
            schema_version=1,
            archive_checksum=checksum,
            created_at=now,
        ),
        completed=True,
    )
    store = CoordinatorStore(remote, archive)
    importer = CoordinatorImporter(session)
    exporter = CoordinatorExporter(now)
    lock_session = Session(engine)
    coordinator = BackupCoordinator(
        session_factory=lambda: session,
        lock_session_factory=lambda: lock_session,
        exporter_factory=lambda session: exporter,
        importer_factory=lambda session: importer,
        store_factory=lambda session, user, connection: store,
        clock=lambda: now,
        temporary_directory=tmp_path / "coordinator-temp",
        close_sessions=False,
    )
    try:
        yield {
            "user": user,
            "other_user": other_user,
            "source": source,
            "other_source": other_source,
            "connection": connection,
            "remote": remote,
            "store": store,
            "importer": importer,
            "exporter": exporter,
            "coordinator": coordinator,
            "archive_owner_id": archive_owner_id,
            "drive_owner_id": drive_owner_id,
            "session": session,
        }
    finally:
        lock_session.close()


@pytest.mark.asyncio
async def test_preview_restore_uses_trusted_remote_workspace_owner(
    coordinator_restore: dict[str, Any],
) -> None:
    source: WorkspaceBackup = coordinator_restore["source"]
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    user: User = coordinator_restore["user"]

    preview = await coordinator.preview_restore(user.id, source.backup_id)  # type: ignore[arg-type]

    assert preview.schema_version == 1
    assert coordinator_restore["importer"].preview_owner_ids == [
        coordinator_restore["archive_owner_id"]
    ]
    assert coordinator_restore["importer"].preview_backup_ids == [
        coordinator_restore["remote"].metadata.backup_id
    ]
    assert coordinator_restore["archive_owner_id"] != user.portable_id
    assert coordinator_restore["store"].listed_owner_ids == [
        coordinator_restore["drive_owner_id"]
    ]


@pytest.mark.asyncio
async def test_preview_allows_oauth_refresh_without_mutating_workspace_or_files(
    coordinator_restore: dict[str, Any],
    tmp_path: Path,
) -> None:
    session: Session = coordinator_restore["session"]
    user: User = coordinator_restore["user"]
    assert user.id is not None
    existing_file = tmp_path / "preview-existing.txt"
    existing_file.write_bytes(b"workspace bytes")
    note = Notes(
        user_id=user.id,
        title="Preview sentinel",
        content="must remain unchanged",
    )
    document = Document(
        user_id=user.id,
        title="Preview document",
        file_name=existing_file.name,
        file_path=str(existing_file),
        file_size=existing_file.stat().st_size,
        file_type="txt",
        mime_type="text/plain",
    )
    settings_row = UserSettings(user_id=user.id, llm_model="preview-model")
    session.add_all([note, document, settings_row])
    session.commit()
    before = _snapshot_workspace(session, user.id)
    before_bytes = existing_file.read_bytes()
    refreshed_at = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)
    store = OAuthRefreshingCoordinatorStore(
        coordinator_restore["remote"],
        coordinator_restore["store"].archive,
        session,
        coordinator_restore["connection"],
        refreshed_at,
    )
    coordinator = BackupCoordinator(
        session_factory=lambda: session,
        importer_factory=lambda session: coordinator_restore[
            "importer"
        ],
        store_factory=lambda session, user, connection: store,
        temporary_directory=tmp_path / "preview-temp",
        close_sessions=False,
    )
    source: WorkspaceBackup = coordinator_restore["source"]

    preview = await coordinator.preview_restore(
        user.id, source.backup_id
    )

    assert preview.schema_version == 1
    session.refresh(coordinator_restore["connection"])
    assert (
        coordinator_restore["connection"].token_expires_at
        == refreshed_at
    )
    assert _snapshot_workspace(session, user.id) == before
    assert existing_file.read_bytes() == before_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", ["restore", " RESTORE", "RESTORE ", ""])
async def test_restore_requires_exact_confirmation_before_side_effects(
    coordinator_restore: dict[str, Any], confirmation: str
) -> None:
    source: WorkspaceBackup = coordinator_restore["source"]
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    user: User = coordinator_restore["user"]

    with pytest.raises(BackupPreconditionError):
        await coordinator.restore(user.id, source.backup_id, confirmation)  # type: ignore[arg-type]

    assert coordinator_restore["store"].downloaded_ids == []
    assert coordinator_restore["importer"].restore_owner_ids == []


@pytest.mark.asyncio
async def test_restore_creates_verified_safety_backup_and_separate_operation(
    coordinator_restore: dict[str, Any],
) -> None:
    session: Session = coordinator_restore["session"]
    source: WorkspaceBackup = coordinator_restore["source"]
    source_state = (
        source.status,
        source.remote_file_id,
        source.checksum,
        source.completed_at,
    )
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    user: User = coordinator_restore["user"]

    response = await coordinator.restore(user.id, source.backup_id, "RESTORE")  # type: ignore[arg-type]

    session.refresh(source)
    operations = session.exec(
        select(WorkspaceBackup)
        .where(WorkspaceBackup.user_id == user.id)
        .order_by(col(WorkspaceBackup.id))
    ).all()
    safety = next(
        operation
        for operation in operations
        if operation.operation_kind == BackupOperationKind.snapshot
        and operation.backup_id != source.backup_id
    )
    restore_operation = next(
        operation
        for operation in operations
        if operation.operation_kind == BackupOperationKind.restore
    )
    assert response.status == BackupStatus.completed
    assert response.backup_id == restore_operation.backup_id
    assert restore_operation.source_backup_id == source.backup_id
    assert restore_operation.status == BackupStatus.completed
    assert safety.status == BackupStatus.completed
    assert safety.remote_file_id is not None
    assert (
        source.status,
        source.remote_file_id,
        source.checksum,
        source.completed_at,
    ) == source_state
    assert coordinator_restore["importer"].restore_owner_ids == [
        coordinator_restore["archive_owner_id"]
    ]
    assert coordinator_restore["importer"].restore_backup_ids == [
        coordinator_restore["remote"].metadata.backup_id
    ]


@pytest.mark.asyncio
async def test_coordinator_does_not_commit_after_atomic_importer_success(
    coordinator_restore: dict[str, Any],
) -> None:
    importer: CoordinatorImporter = coordinator_restore["importer"]
    importer.reject_commit_after_restore = True
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    source: WorkspaceBackup = coordinator_restore["source"]
    user: User = coordinator_restore["user"]

    response = await coordinator.restore(
        user.id, source.backup_id, "RESTORE"  # type: ignore[arg-type]
    )

    assert response.status == BackupStatus.completed
    restore_operation = coordinator_restore["session"].exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user.id,
            WorkspaceBackup.operation_kind == BackupOperationKind.restore,
        )
    ).one()
    assert restore_operation.status == BackupStatus.completed


@pytest.mark.asyncio
async def test_restore_aborts_when_safety_backup_fails(
    coordinator_restore: dict[str, Any],
) -> None:
    coordinator_restore["exporter"].fail = True
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    source: WorkspaceBackup = coordinator_restore["source"]
    user: User = coordinator_restore["user"]

    with pytest.raises(BackupRestoreFailed):
        await coordinator.restore(user.id, source.backup_id, "RESTORE")  # type: ignore[arg-type]

    assert coordinator_restore["importer"].restore_owner_ids == []
    coordinator_restore["session"].refresh(source)
    assert source.status == BackupStatus.completed


@pytest.mark.asyncio
async def test_restore_failure_marks_only_restore_operation_failed(
    coordinator_restore: dict[str, Any],
) -> None:
    coordinator_restore["importer"].fail_restore = True
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    source: WorkspaceBackup = coordinator_restore["source"]
    user: User = coordinator_restore["user"]

    with pytest.raises(BackupRestoreFailed):
        await coordinator.restore(user.id, source.backup_id, "RESTORE")  # type: ignore[arg-type]

    session: Session = coordinator_restore["session"]
    session.refresh(source)
    restore_operation = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user.id,
            WorkspaceBackup.operation_kind == BackupOperationKind.restore,
        )
    ).one()
    assert source.status == BackupStatus.completed
    assert restore_operation.status == BackupStatus.failed
    assert restore_operation.failure_message == "Workspace restore failed"


@pytest.mark.asyncio
async def test_restore_commit_failure_rolls_back_workspace_and_marks_operation_failed(
    session: Session, restore_workspace: dict[str, Any]
) -> None:
    user: User = restore_workspace["user"]
    assert user.id is not None
    before = _snapshot_workspace(session, user.id)
    now = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=b"encrypted",
        google_subject="commit-failure-subject",
        google_email=user.email,
        granted_scopes=["https://www.googleapis.com/auth/drive.appdata"],
    )
    schedule = BackupSchedule(user_id=user.id, enabled=True)
    archive: Path = restore_workspace["archive"]
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = WorkspaceBackup(
        backup_id=restore_workspace["archive_backup_id"],
        user_id=user.id,
        status=BackupStatus.completed,
        trigger=BackupTrigger.manual,
        remote_file_id="commit-failure-source",
        archive_size_bytes=archive.stat().st_size,
        checksum=checksum,
        schema_version=1,
        completed_at=now,
    )
    session.add_all([connection, schedule, source])
    session.commit()
    remote = StoredBackup(
        remote_id="commit-failure-source",
        name=archive.name,
        size=archive.stat().st_size,
        created_at=now,
        metadata=BackupObjectMetadata(
            drive_owner_id=derive_drive_owner_id(
                connection.google_subject
            ),
            workspace_owner_id=restore_workspace["archive_owner_id"],
            backup_id=source.backup_id,
            schema_version=1,
            archive_checksum=checksum,
            created_at=now,
        ),
        completed=True,
    )
    store = CoordinatorStore(remote, archive)
    commit_attempted = Event()

    class CommitFailingImporter(BackupImporter):
        def _insert_workspace(
            self,
            user_id: int,
            validated: Any,
            final_paths: Mapping[UUID, Path],
        ) -> list[int]:
            result = super()._insert_workspace(
                user_id, validated, final_paths
            )

            def fail_commit(database_session: Session) -> None:
                commit_attempted.set()
                raise RuntimeError("injected atomic restore commit failure")

            assert self.session is not None
            event.listen(self.session, "before_commit", fail_commit, once=True)
            return result

    with Session(engine) as lock_session:
        coordinator = BackupCoordinator(
            session_factory=lambda: session,
            lock_session_factory=lambda: lock_session,
            exporter_factory=lambda session: CoordinatorExporter(
                now
            ),
            importer_factory=lambda session: CommitFailingImporter(
                session=session,
                upload_root=restore_workspace["upload_root"],
                temporary_directory=restore_workspace["temp_root"],
                supported_app_versions={"0.1.0"},
            ),
            store_factory=lambda session, user, connection: store,
            clock=lambda: now,
            temporary_directory=restore_workspace["temp_root"]
            / "coordinator",
            close_sessions=False,
        )

        with pytest.raises(BackupRestoreFailed):
            await coordinator.restore(
                user.id, source.backup_id, "RESTORE"
            )

    assert commit_attempted.is_set()
    assert _snapshot_workspace(session, user.id) == before
    restore_operation = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user.id,
            WorkspaceBackup.operation_kind == BackupOperationKind.restore,
        )
    ).one()
    assert restore_operation.status == BackupStatus.failed
    assert restore_operation.failure_message == "Workspace restore failed"
    assert sorted(
        path.name for path in restore_workspace["upload_root"].iterdir()
    ) == ["old.txt", "other.txt"]


@pytest.mark.asyncio
async def test_restore_rejects_wrong_user_and_wrong_drive_owner(
    coordinator_restore: dict[str, Any],
) -> None:
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    other_user: User = coordinator_restore["other_user"]
    source: WorkspaceBackup = coordinator_restore["source"]

    with pytest.raises(BackupPreconditionError):
        await coordinator.preview_restore(other_user.id, source.backup_id)  # type: ignore[arg-type]

    remote: StoredBackup = coordinator_restore["remote"]
    coordinator_restore["store"].backups[0] = StoredBackup(
        remote_id=remote.remote_id,
        name=remote.name,
        size=remote.size,
        created_at=remote.created_at,
        metadata=BackupObjectMetadata(
            drive_owner_id=uuid4(),
            workspace_owner_id=remote.metadata.workspace_owner_id,
            backup_id=remote.metadata.backup_id,
            schema_version=remote.metadata.schema_version,
            archive_checksum=remote.metadata.archive_checksum,
            created_at=remote.metadata.created_at,
        ),
        completed=True,
    )
    with pytest.raises(BackupPreconditionError):
        await coordinator.preview_restore(
            coordinator_restore["user"].id, source.backup_id
        )


@pytest.mark.asyncio
async def test_preview_rejects_download_checksum_mismatch(
    coordinator_restore: dict[str, Any],
) -> None:
    coordinator_restore["store"].archive.write_bytes(b"tampered archive")
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]
    source: WorkspaceBackup = coordinator_restore["source"]
    user: User = coordinator_restore["user"]

    with pytest.raises(BackupPreconditionError):
        await coordinator.preview_restore(user.id, source.backup_id)  # type: ignore[arg-type]

    assert coordinator_restore["importer"].preview_owner_ids == []


def test_pending_restore_operation_blocks_new_snapshot(
    coordinator_restore: dict[str, Any],
) -> None:
    session: Session = coordinator_restore["session"]
    user: User = coordinator_restore["user"]
    assert user.id is not None
    source: WorkspaceBackup = coordinator_restore["source"]
    restore_operation = WorkspaceBackup(
        user_id=user.id,
        operation_kind=BackupOperationKind.restore,
        source_backup_id=source.backup_id,
        status=BackupStatus.pending,
    )
    session.add(restore_operation)
    session.commit()
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]

    with pytest.raises(BackupPreconditionError):
        coordinator.start_backup(user.id, BackupTrigger.manual)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_backup_rejects_restore_operation(
    coordinator_restore: dict[str, Any],
) -> None:
    session: Session = coordinator_restore["session"]
    user: User = coordinator_restore["user"]
    assert user.id is not None
    source: WorkspaceBackup = coordinator_restore["source"]
    restore_operation = WorkspaceBackup(
        user_id=user.id,
        operation_kind=BackupOperationKind.restore,
        source_backup_id=source.backup_id,
        status=BackupStatus.pending,
    )
    session.add(restore_operation)
    session.commit()
    coordinator: BackupCoordinator = coordinator_restore["coordinator"]

    with pytest.raises(BackupPreconditionError):
        await coordinator.run_backup(restore_operation.backup_id)
