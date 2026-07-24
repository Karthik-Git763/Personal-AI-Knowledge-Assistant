from typing import cast

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import String, inspect, text
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel

from app.core.database import engine

PORTABLE_ID_TABLES = {
    "users",
    "note_templates",
    "notes",
    "note_folders",
    "note_tags",
    "note_links",
    "documents",
    "chat_sessions",
    "chat_messages",
}
BACKUP_TABLES = {
    "google_drive_connections",
    "oauth_states",
    "workspace_backups",
    "backup_schedules",
}


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    return config


def test_google_drive_backup_foundation_is_the_migration_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_current_head() == "0005_google_drive_backup_foundation"
    assert script.get_revision("0005_google_drive_backup_foundation").down_revision == "0004_streaming_chat"


def _seed_pre_backup_rows(connection: Connection) -> None:
    user_id = connection.execute(
        text(
            """
            INSERT INTO users (
                email, is_active, is_superuser, created_at, updated_at, hashed_password, is_verified, is_deleted
            ) VALUES (
                'backup@example.com', true, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'hashed-password', false, false
            ) RETURNING id
            """
        )
    ).scalar_one()
    folder_id = connection.execute(
        text(
            """
            INSERT INTO note_folders (
                created_at, updated_at, user_id, name, is_shared, is_archived, sort_order, is_deleted
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :user_id, 'Folder', false, false, 0, false
            ) RETURNING id
            """
        ),
        {"user_id": user_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO documents (
                created_at, updated_at, user_id, title, file_name, file_path, file_size, file_type, mime_type,
                is_deleted, language, status, chunk_count, last_accessed_at
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :user_id, 'Document', 'document.txt', 'uploads/document.txt',
                1, 'txt', 'text/plain', false, 'en', 'completed', 0, CURRENT_TIMESTAMP
            )
            """
        ),
        {"user_id": user_id},
    )
    chat_session_id = connection.execute(
        text(
            """
            INSERT INTO chat_sessions (
                created_at, updated_at, user_id, is_archived, is_pinned, last_message_at
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :user_id, false, false, CURRENT_TIMESTAMP
            ) RETURNING id
            """
        ),
        {"user_id": user_id},
    ).scalar_one()
    first_note_id = connection.execute(
        text(
            """
            INSERT INTO notes (
                created_at, updated_at, user_id, folder_id, title, content, content_type, ai_generated,
                is_favorite, is_archived, is_pinned, version, is_public, is_locked, is_deleted,
                last_accessed_at, last_edited_at
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :user_id, :folder_id, 'One', '', 'markdown', false,
                false, false, false, 1, false, false, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id
            """
        ),
        {"user_id": user_id, "folder_id": folder_id},
    ).scalar_one()
    second_note_id = connection.execute(
        text(
            """
            INSERT INTO notes (
                created_at, updated_at, user_id, folder_id, title, content, content_type, ai_generated,
                is_favorite, is_archived, is_pinned, version, is_public, is_locked, is_deleted,
                last_accessed_at, last_edited_at
            ) VALUES (
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :user_id, :folder_id, 'Two', '', 'markdown', false,
                false, false, false, 1, false, false, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id
            """
        ),
        {"user_id": user_id, "folder_id": folder_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO note_tags (user_id, name, created_at)
            VALUES (:user_id, 'Tag', CURRENT_TIMESTAMP)
            """
        ),
        {"user_id": user_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO note_links (source_note_id, target_note_id, link_type, created_at)
            VALUES (:first_note_id, :second_note_id, 'related', CURRENT_TIMESTAMP)
            """
        ),
        {"first_note_id": first_note_id, "second_note_id": second_note_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO chat_messages (created_at, updated_at, session_id, role, content)
            VALUES (CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :chat_session_id, 'user', 'Hello')
            """
        ),
        {"chat_session_id": chat_session_id},
    )


def test_google_drive_backup_upgrade_creates_schema() -> None:
    config = _alembic_config()

    SQLModel.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    command.upgrade(config, "0004_streaming_chat")
    with engine.begin() as connection:
        _seed_pre_backup_rows(connection)
    command.upgrade(config, "head")

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert BACKUP_TABLES.issubset(table_names)

    connection_columns = {
        column["name"]: column for column in inspector.get_columns("google_drive_connections")
    }
    connection_status_type = cast(String, connection_columns["status"]["type"])
    assert connection_status_type.length is not None
    assert connection_status_type.length >= len("reauthorization_required")
    backup_columns = {column["name"]: column for column in inspector.get_columns("workspace_backups")}
    assert backup_columns["backup_id"]["nullable"] is False
    assert backup_columns["operation_kind"]["nullable"] is False
    assert backup_columns["source_backup_id"]["nullable"] is True
    backup_indexes = {index["name"]: index for index in inspector.get_indexes("workspace_backups")}
    assert backup_indexes["ix_workspace_backups_backup_id"]["unique"] is True

    for table_name in PORTABLE_ID_TABLES:
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert columns["portable_id"]["nullable"] is False
        indexes = {index["name"]: index for index in inspector.get_indexes(table_name)}
        assert indexes[f"ix_{table_name}_portable_id"]["unique"] is True
        with engine.connect() as connection:
            assert connection.execute(
                text(f"SELECT count(*) FROM {table_name} WHERE portable_id IS NULL")
            ).scalar_one() == 0

    command.downgrade(config, "0004_streaming_chat")

    inspector = inspect(engine)
    assert BACKUP_TABLES.isdisjoint(inspector.get_table_names())
    for table_name in PORTABLE_ID_TABLES:
        assert "portable_id" not in {column["name"] for column in inspector.get_columns(table_name)}

    with engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT character_maximum_length
                FROM information_schema.columns
                WHERE table_name = 'alembic_version' AND column_name = 'version_num'
                """
            )
        ).scalar_one() == 64
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0004_streaming_chat"
