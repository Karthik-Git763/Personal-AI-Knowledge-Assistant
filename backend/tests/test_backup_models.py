from uuid import UUID, uuid4

import pytest
from sqlmodel import SQLModel

from app.models.backup import (
    BackupSchedule,
    BackupStatus,
    BackupTrigger,
    DriveConnectionStatus,
    GoogleDriveConnection,
    OAuthState,
    WorkspaceBackup,
)
from app.models.chat import ChatMessages, ChatRole, ChatSession
from app.models.document import Document
from app.models.note import NoteCategory, NoteFolders, NoteLinks, Notes, NoteTags, NoteTemplates
from app.models.user import User


def test_portable_ids_are_generated_and_distinct() -> None:
    first = Notes(user_id=1, title="One", content="")
    second = Notes(user_id=1, title="Two", content="")

    assert first.portable_id is not None
    assert first.portable_id != second.portable_id


def test_all_backupable_models_generate_portable_ids() -> None:
    models = [
        User(email="portable@example.com", hashed_password="hashed-password"),
        NoteFolders(user_id=1, name="Folder"),
        NoteTags(user_id=1, name="Tag"),
        NoteLinks(source_note_id=1, target_note_id=2),
        Document(
            user_id=1,
            title="Document",
            file_name="document.txt",
            file_path="uploads/document.txt",
            file_size=1,
            file_type="txt",
            mime_type="text/plain",
        ),
        ChatSession(user_id=1),
        ChatMessages(session_id=1, role=ChatRole.user, content="Hello"),
        NoteTemplates(user_id=1, name="Template", category=NoteCategory.other, content="Content"),
    ]

    portable_ids = [model.portable_id for model in models]

    assert all(isinstance(portable_id, UUID) for portable_id in portable_ids)
    assert len(set(portable_ids)) == len(portable_ids)


def test_backup_enums_use_persisted_values() -> None:
    assert [status.value for status in BackupStatus] == [
        "pending",
        "exporting",
        "uploading",
        "completed",
        "failed",
    ]
    assert [trigger.value for trigger in BackupTrigger] == ["manual", "scheduled"]
    assert [status.value for status in DriveConnectionStatus] == [
        "connected",
        "disconnected",
        "reauthorization_required",
        "failed",
    ]


def test_backup_defaults_to_pending_manual_snapshot() -> None:
    backup = WorkspaceBackup(user_id=1)

    assert backup.status is BackupStatus.pending
    assert backup.trigger is BackupTrigger.manual
    assert isinstance(backup.backup_id, UUID)
    assert backup.schema_version == 1
    assert backup.item_counts == {}


def test_backup_failure_message_is_sanitized_on_construction_and_update() -> None:
    backup = WorkspaceBackup(user_id=1, failure_message=" <b>Backup failed</b>\nagain ")

    assert backup.failure_message == "Backup failed again"

    backup.failure_message = " <script>ignore</script> Update failed "
    assert backup.failure_message == "ignore Update failed"

    backup.sqlmodel_update({"failure_message": " <i>Retry</i>\nlater "})
    assert backup.failure_message == "Retry later"


def test_backup_schedule_defaults_to_daily_enabled() -> None:
    schedule = BackupSchedule(user_id=1)

    assert schedule.enabled is True
    assert schedule.interval_hours == 24


def test_backup_tables_are_registered() -> None:
    assert {
        GoogleDriveConnection.__tablename__,
        OAuthState.__tablename__,
        WorkspaceBackup.__tablename__,
        BackupSchedule.__tablename__,
    }.issubset(SQLModel.metadata.tables)


def test_portable_ids_are_immutable_after_persistence(session) -> None:
    user = User(email="portable@example.com", hashed_password="hashed-password")
    folder = NoteFolders(user_id=1, name="Folder", portable_id=uuid4())
    tag = NoteTags(user_id=1, name="Tag", portable_id=uuid4())
    document = Document(
        user_id=1,
        title="Document",
        file_name="document.txt",
        file_path="uploads/document.txt",
        file_size=1,
        file_type="txt",
        mime_type="text/plain",
        portable_id=uuid4(),
    )
    chat_session = ChatSession(user_id=1, portable_id=uuid4())
    template = NoteTemplates(
        user_id=1,
        name="Template",
        category=NoteCategory.other,
        content="Content",
        portable_id=uuid4(),
    )
    first_note = Notes(user_id=1, title="One", content="", portable_id=uuid4())
    second_note = Notes(user_id=1, title="Two", content="", portable_id=uuid4())

    session.add(user)
    session.flush()
    session.add_all([folder, tag, document, chat_session, template, first_note, second_note])
    session.flush()

    note_link = NoteLinks(
        source_note_id=first_note.id,
        target_note_id=second_note.id,
        portable_id=uuid4(),
    )
    chat_message = ChatMessages(
        session_id=chat_session.id,
        role=ChatRole.user,
        content="Hello",
        portable_id=uuid4(),
    )
    session.add_all([note_link, chat_message])
    session.commit()

    models = [user, folder, tag, document, chat_session, template, first_note, second_note, note_link, chat_message]
    original_ids = {id(model): model.portable_id for model in models}

    for model in models:
        assert model.portable_id == original_ids[id(model)]
        with pytest.raises(ValueError, match="portable_id is immutable"):
            model.portable_id = uuid4()
        with pytest.raises(ValueError, match="portable_id is immutable"):
            model.sqlmodel_update({"portable_id": uuid4()})
