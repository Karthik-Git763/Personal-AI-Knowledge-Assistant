from uuid import UUID

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
from app.models.note import NoteFolders, NoteLinks, Notes, NoteTags


def test_portable_ids_are_generated_and_distinct() -> None:
    first = Notes(user_id=1, title="One", content="")
    second = Notes(user_id=1, title="Two", content="")

    assert first.portable_id is not None
    assert first.portable_id != second.portable_id


def test_all_backupable_models_generate_portable_ids() -> None:
    models = [
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
    ]

    portable_ids = [model.portable_id for model in models]

    assert all(isinstance(portable_id, UUID) for portable_id in portable_ids)
    assert len(set(portable_ids)) == len(portable_ids)


def test_backup_enums_use_persisted_values() -> None:
    assert [status.value for status in BackupStatus] == ["pending", "running", "completed", "failed"]
    assert [trigger.value for trigger in BackupTrigger] == ["manual", "scheduled"]
    assert [status.value for status in DriveConnectionStatus] == ["connected", "disconnected", "failed"]


def test_backup_defaults_to_pending_manual_snapshot() -> None:
    backup = WorkspaceBackup(user_id=1)

    assert backup.status is BackupStatus.pending
    assert backup.trigger is BackupTrigger.manual
    assert backup.schema_version == 1
    assert backup.item_counts == {}


def test_backup_failure_message_is_sanitized() -> None:
    backup = WorkspaceBackup(user_id=1, failure_message=" <b>Backup failed</b>\nagain ")

    assert backup.failure_message == "Backup failed again"


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
