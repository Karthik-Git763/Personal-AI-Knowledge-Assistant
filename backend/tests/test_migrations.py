from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from app.core.database import engine


def _alembic_config() -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    return config


def test_google_drive_backup_foundation_is_the_migration_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_current_head() == "0005_google_drive_backup_foundation"


def test_google_drive_backup_upgrade_creates_schema() -> None:
    config = _alembic_config()

    SQLModel.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    command.upgrade(config, "0004_streaming_chat")
    command.upgrade(config, "head")

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    assert {
        "google_drive_connections",
        "oauth_states",
        "workspace_backups",
        "backup_schedules",
    }.issubset(table_names)

    for table_name in {
        "notes",
        "note_folders",
        "note_tags",
        "note_links",
        "documents",
        "chat_sessions",
        "chat_messages",
    }:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert "portable_id" in columns
