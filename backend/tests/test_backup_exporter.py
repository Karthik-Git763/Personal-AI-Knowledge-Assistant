import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlmodel import Session, select

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
from app.services import backup_archive
from app.services import backup_exporter as backup_exporter_module
from app.services.backup_archive import (
    ArchiveSizeExceeded,
    DuplicateArchiveName,
    UnsafeBackupPath,
    VersionedZipWriter,
)
from app.services.backup_exporter import BackupExporter
from app.services.backup_importer import BackupImporter


@pytest.fixture
def populated_workspace(session: Session, tmp_path: Path) -> tuple[User, Path]:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    uploaded_file = upload_root / "original report.txt"
    uploaded_file.write_bytes(b"the original upload")

    user = User(email="backup@example.com", hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None

    folder = NoteFolders(user_id=user.id, name="Research", is_archived=True)
    tag = NoteTags(user_id=user.id, name="important")
    document = Document(
        user_id=user.id,
        title="Report",
        file_name="../original report.txt",
        file_path=str(uploaded_file),
        file_size=uploaded_file.stat().st_size,
        file_type=".txt",
        mime_type="text/plain",
        content="derived extraction",
        content_preview="derived preview",
        summary="derived summary",
        chunk_count=1,
    )
    chat = ChatSession(user_id=user.id, title="Discussion", is_archived=True)
    session.add_all([folder, tag, document, chat])
    session.commit()
    session.refresh(folder)
    session.refresh(tag)
    session.refresh(document)
    session.refresh(chat)

    note = Notes(
        user_id=user.id,
        folder_id=folder.id,
        title="Current",
        content="Durable content",
        linked_document_id=document.id,
        linked_chat_session_id=chat.id,
    )
    historical_note = Notes(user_id=user.id, title="Previous", content="Version content", is_archived=True)
    session.add_all([note, historical_note])
    session.commit()
    session.refresh(note)
    session.refresh(historical_note)
    assert note.id is not None
    assert historical_note.id is not None
    assert tag.id is not None

    note.previous_version_id = historical_note.id
    link = NoteLinks(source_note_id=note.id, target_note_id=historical_note.id)
    relation = NoteTagRelations(note_id=note.id, tag_id=tag.id)
    template = NoteTemplates(
        user_id=user.id,
        name="Template",
        category=NoteCategory.other,
        content="Template content",
    )
    message = ChatMessages(session_id=chat.id, role=ChatRole.user, content="Remember this")
    settings = UserSettings(user_id=user.id, default_note_folder_id=folder.id, language="fr")
    chunk = DocumentChunks(
        document_id=document.id,
        chunk_index=0,
        content="must never export",
        vector_id="must-never-export",
    )
    session.add_all([link, relation, template, message, settings, chunk])
    session.add(note)
    session.commit()
    return user, upload_root


@pytest.fixture
def exporter(session: Session, tmp_path: Path) -> BackupExporter:
    return BackupExporter(
        session=session,
        upload_root=tmp_path / "uploads",
        maximum_archive_size=10_000_000,
        app_version="test-version",
    )


def test_export_contains_durable_records_and_original_file(
    populated_workspace: tuple[User, Path], exporter: BackupExporter, tmp_path: Path
) -> None:
    user, _ = populated_workspace

    result = exporter.export(user, tmp_path / "backup.zip")

    with ZipFile(result.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["counts"]["notes"] == 2
        assert manifest["counts"]["documents"] == 1
        assert manifest["app_version"] == "test-version"
        assert any(name.startswith("files/documents/") for name in archive.namelist())
        assert "document_chunks.json" not in archive.namelist()


def test_export_uses_supplied_archive_identity(
    populated_workspace: tuple[User, Path], exporter: BackupExporter, tmp_path: Path
) -> None:
    user, _ = populated_workspace
    expected_backup_id = uuid4()

    result = exporter.export(
        user,
        tmp_path / "identity.zip",
        backup_id=expected_backup_id,
    )

    assert result.manifest.backup_id == expected_backup_id


def test_export_uses_portable_uuid_relationships_and_excludes_derived_content(
    populated_workspace: tuple[User, Path], exporter: BackupExporter, tmp_path: Path
) -> None:
    user, _ = populated_workspace

    result = exporter.export(user, tmp_path / "backup.zip")

    with ZipFile(result.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        notes = json.loads(archive.read("notes.json"))
        documents = json.loads(archive.read("documents.json"))
        relations = json.loads(archive.read("note_tag_relations.json"))
        templates = json.loads(archive.read("templates.json"))
        session_template = exporter.session.exec(select(NoteTemplates)).one()
        assert manifest["owner_id"] == str(user.portable_id)
        assert templates[0]["id"] == str(session_template.portable_id)
        linked_note = next(note for note in notes if note["folder_id"] is not None)
        assert UUID(linked_note["folder_id"])
        assert UUID(linked_note["linked_document_id"])
        assert UUID(linked_note["linked_chat_session_id"])
        assert UUID(relations[0]["note_id"])
        assert UUID(relations[0]["tag_id"])
        assert "content" not in documents[0]
        assert "content_preview" not in documents[0]
        assert "summary" not in documents[0]
        assert "file_path" not in documents[0]
        assert "vector_id" not in archive.read("documents.json").decode()


def test_export_writes_deterministic_record_json_and_entry_checksums(
    populated_workspace: tuple[User, Path], exporter: BackupExporter, tmp_path: Path
) -> None:
    user, _ = populated_workspace

    result = exporter.export(user, tmp_path / "backup.zip")
    second_result = exporter.export(user, tmp_path / "second-backup.zip")

    with ZipFile(result.path) as archive, ZipFile(second_result.path) as second_archive:
        manifest = json.loads(archive.read("manifest.json"))
        notes = archive.read("notes.json")
        file_name = next(name for name in archive.namelist() if name.startswith("files/documents/"))
        assert notes == json.dumps(
            json.loads(notes), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        assert manifest["checksums"]["notes.json"] == hashlib.sha256(notes).hexdigest()
        assert manifest["checksums"][file_name] == hashlib.sha256(archive.read(file_name)).hexdigest()
        assert archive.namelist() == second_archive.namelist()
        for name in archive.namelist():
            if name != "manifest.json":
                assert archive.read(name) == second_archive.read(name)
                assert archive.getinfo(name).date_time == second_archive.getinfo(name).date_time
                assert archive.getinfo(name).external_attr == second_archive.getinfo(name).external_attr
    assert result.archive_checksum == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert result.archive_size == result.path.stat().st_size


def test_export_rejects_document_path_outside_upload_root(exporter: BackupExporter) -> None:
    with pytest.raises(UnsafeBackupPath):
        exporter.validate_source_path(Path("../secret.txt"))


def test_export_includes_only_soft_deleted_notes_needed_by_live_links(
    populated_workspace: tuple[User, Path],
    exporter: BackupExporter,
    session: Session,
    tmp_path: Path,
) -> None:
    user, _ = populated_workspace
    active_note = session.exec(select(Notes).where(Notes.title == "Current")).one()
    linked_deleted_note = Notes(
        user_id=user.id,
        title="Linked deleted",
        content="Keep for the link",
        is_deleted=True,
    )
    unrelated_deleted_note = Notes(
        user_id=user.id,
        title="Unrelated deleted",
        content="Do not keep",
        is_deleted=True,
    )
    session.add_all([linked_deleted_note, unrelated_deleted_note])
    session.commit()
    session.refresh(linked_deleted_note)
    session.refresh(unrelated_deleted_note)
    session.add(NoteLinks(source_note_id=active_note.id, target_note_id=linked_deleted_note.id))
    session.commit()

    result = exporter.export(user, tmp_path / "soft-deleted.zip")

    with ZipFile(result.path) as archive:
        exported_ids = {record["id"] for record in json.loads(archive.read("notes.json"))}
    assert str(linked_deleted_note.portable_id) in exported_ids
    assert str(unrelated_deleted_note.portable_id) not in exported_ids


def test_export_includes_complete_deleted_note_version_chain(
    populated_workspace: tuple[User, Path],
    exporter: BackupExporter,
    session: Session,
    tmp_path: Path,
) -> None:
    user, upload_root = populated_workspace
    oldest = Notes(user_id=user.id, title="Oldest", content="v1", is_deleted=True)
    previous = Notes(user_id=user.id, title="Previous deleted", content="v2", is_deleted=True)
    session.add_all([oldest, previous])
    session.commit()
    previous.previous_version_id = oldest.id
    current = session.exec(
        select(Notes).where(Notes.user_id == user.id, Notes.title == "Current")
    ).one()
    current.previous_version_id = previous.id
    session.add_all([current, previous])
    session.commit()

    result = exporter.export(user, tmp_path / "version-chain.zip")

    with ZipFile(result.path) as archive:
        exported_ids = {record["id"] for record in json.loads(archive.read("notes.json"))}
    assert {
        str(current.portable_id),
        str(previous.portable_id),
        str(oldest.portable_id),
    } <= exported_ids
    preview = BackupImporter(
        session=session,
        upload_root=upload_root,
        supported_app_versions={exporter.app_version},
    ).preview(result.path, expected_workspace_owner_id=user.portable_id)
    assert preview.item_counts["notes"] == len(exported_ids)


def test_export_includes_deleted_ancestor_chain_for_exported_folder(
    populated_workspace: tuple[User, Path],
    exporter: BackupExporter,
    session: Session,
    tmp_path: Path,
) -> None:
    user, _ = populated_workspace
    root = NoteFolders(user_id=user.id, name="Deleted root", is_deleted=True)
    session.add(root)
    session.commit()
    parent = NoteFolders(user_id=user.id, name="Deleted parent", parent_folder_id=root.id, is_deleted=True)
    session.add(parent)
    session.flush()
    child = NoteFolders(user_id=user.id, name="Live child", parent_folder_id=parent.id)
    session.add(child)
    session.commit()
    note = session.exec(select(Notes).where(Notes.user_id == user.id, Notes.title == "Current")).one()
    note.folder_id = child.id
    session.add(note)
    session.commit()

    result = exporter.export(user, tmp_path / "ancestor-chain.zip")

    with ZipFile(result.path) as archive:
        folders = json.loads(archive.read("folders.json"))
    exported_ids = {folder["id"] for folder in folders}
    assert {str(root.portable_id), str(parent.portable_id), str(child.portable_id)} <= exported_ids


def test_export_includes_deleted_default_preference_folder(
    populated_workspace: tuple[User, Path],
    exporter: BackupExporter,
    session: Session,
    tmp_path: Path,
) -> None:
    user, _ = populated_workspace
    default_folder = NoteFolders(user_id=user.id, name="Deleted default", is_deleted=True)
    session.add(default_folder)
    session.commit()
    preferences = session.get(UserSettings, user.id)
    assert preferences is not None
    preferences.default_note_folder_id = default_folder.id
    session.add(preferences)
    session.commit()

    result = exporter.export(user, tmp_path / "default-folder.zip")

    with ZipFile(result.path) as archive:
        folders = json.loads(archive.read("folders.json"))
        preferences_record = json.loads(archive.read("user_preferences.json"))[0]
    assert str(default_folder.portable_id) in {folder["id"] for folder in folders}
    assert preferences_record["default_note_folder_id"] == str(default_folder.portable_id)


def test_export_rejects_missing_source_and_removes_partial_archive(
    session: Session, exporter: BackupExporter, tmp_path: Path
) -> None:
    user = User(email="missing@example.com", hashed_password="hashed-password")
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None
    session.add(
        Document(
            user_id=user.id,
            title="Missing",
            file_name="missing.txt",
            file_path=str(tmp_path / "uploads" / "missing.txt"),
            file_size=1,
            file_type=".txt",
            mime_type="text/plain",
        )
    )
    session.commit()

    destination = tmp_path / "missing.zip"
    with pytest.raises(FileNotFoundError):
        exporter.export(user, destination)
    assert not destination.exists()


def test_export_rejects_symlink_source(exporter: BackupExporter, tmp_path: Path) -> None:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret")
    link = upload_root / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(UnsafeBackupPath):
        exporter.validate_source_path(link)


def test_versioned_zip_writer_rejects_duplicate_names_and_size_bound(tmp_path: Path) -> None:
    duplicate_destination = tmp_path / "duplicate.zip"
    with VersionedZipWriter(duplicate_destination, maximum_size=10_000) as archive:
        archive.write_bytes("records.json", b"first")
        with pytest.raises(DuplicateArchiveName):
            archive.write_bytes("records.json", b"second")

    oversized_destination = tmp_path / "oversized.zip"
    with pytest.raises(ArchiveSizeExceeded):
        with VersionedZipWriter(oversized_destination, maximum_size=1) as archive:
            archive.write_bytes("records.json", os.urandom(128))
    assert not oversized_destination.exists()


def test_versioned_zip_writer_streams_json_records_with_stable_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "streamed.zip"

    def records() -> Iterator[Mapping[str, Any]]:
        yield {"id": "b", "value": 2}
        yield {"id": "a", "value": 1}

    checksum = ""
    count = 0
    with VersionedZipWriter(destination, maximum_size=10_000) as archive:
        checksum, count = archive.write_json_array("records.json", records())

    with ZipFile(destination) as archive:
        entry = archive.getinfo("records.json")
        data = archive.read("records.json")
    assert count == 2
    assert checksum == hashlib.sha256(data).hexdigest()
    assert data == b'[{"id":"b","value":2},{"id":"a","value":1}]'
    assert entry.date_time == (1980, 1, 1, 0, 0, 0)
    assert entry.external_attr >> 16 == 0o100644


def test_exporter_uses_yield_per_for_bounded_database_iteration(tmp_path: Path) -> None:
    class ResultDouble:
        def __init__(self) -> None:
            self.batch_size: int | None = None

        def yield_per(self, size: int) -> Iterator[int]:
            self.batch_size = size
            return iter((1, 2))

    class SessionDouble:
        def __init__(self, result: ResultDouble) -> None:
            self.result = result

        def exec(self, _statement: Any) -> ResultDouble:
            return self.result

    result = ResultDouble()
    exporter = BackupExporter(
        session=cast(Session, SessionDouble(result)),
        upload_root=tmp_path,
        maximum_archive_size=10_000,
    )

    assert list(exporter._iterate(select(Notes))) == [1, 2]
    assert result.batch_size == exporter.BATCH_SIZE


def test_validated_source_stream_uses_open_handle_after_path_replacement(tmp_path: Path) -> None:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    source = upload_root / "report.txt"
    source.write_bytes(b"original")

    with backup_archive.open_validated_source(upload_root, source) as input_file:
        source.unlink()
        source.write_bytes(b"replacement")
        with VersionedZipWriter(tmp_path / "same-handle.zip", maximum_size=10_000) as archive:
            archive.write_fileobj("report.txt", input_file)

    with ZipFile(tmp_path / "same-handle.zip") as archive:
        assert archive.read("report.txt") == b"original"


def test_export_rejects_cross_owner_relationship_target(
    populated_workspace: tuple[User, Path], exporter: BackupExporter, session: Session, tmp_path: Path
) -> None:
    user, _ = populated_workspace
    other_user = User(email="other@example.com", hashed_password="hashed-password")
    session.add(other_user)
    session.commit()
    session.refresh(other_user)
    foreign_folder = NoteFolders(user_id=other_user.id, name="Foreign")
    session.add(foreign_folder)
    session.commit()
    note = session.exec(select(Notes).where(Notes.user_id == user.id, Notes.title == "Current")).one()
    note.folder_id = foreign_folder.id
    session.add(note)
    session.commit()

    with pytest.raises(backup_exporter_module.InvalidBackupReference, match="folder"):
        exporter.export(user, tmp_path / "invalid-reference.zip")


def test_versioned_zip_writer_removes_partial_output_after_mid_write_error(tmp_path: Path) -> None:
    class BrokenReader:
        def read(self, size: int) -> bytes:
            raise OSError("read failed")

    destination = tmp_path / "partial.zip"
    with pytest.raises(OSError, match="read failed"):
        with VersionedZipWriter(destination, maximum_size=10_000) as archive:
            archive.write_fileobj("records.bin", cast(BinaryIO, BrokenReader()))
    assert not destination.exists()
