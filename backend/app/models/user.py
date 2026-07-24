from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import field_validator
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import CheckConstraint, Column, Field, Index, Relationship, SQLModel, desc

from app.utils.sanitization import sanitize_plain_text

from .chat import TimestampMixin

if TYPE_CHECKING:
    from .backup import BackupSchedule, GoogleDriveConnection, OAuthState, WorkspaceBackup
    from .chat import ChatSession
    from .document import Document
    from .note import NoteCollaborators, NoteFolders, Notes, NoteTags, NoteTemplates


# Shared property
class UserBase(SQLModel):
    email: str = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("full_name", mode="before")
    @classmethod
    def sanitize_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return sanitize_plain_text(v)

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: str) -> str:
        return sanitize_plain_text(v)


# Creation API properties
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRegister(SQLModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("full_name", mode="before")
    @classmethod
    def sanitize_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return sanitize_plain_text(v)

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: str) -> str:
        return sanitize_plain_text(v)


# Update API properties | Optional
class UserUpdate(SQLModel):
    email: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    is_superuser: bool | None = None
    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("full_name", mode="before")
    @classmethod
    def sanitize_full_name(cls, value: str | None) -> str | None:
        return sanitize_plain_text(value) if value is not None else None

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, value: str | None) -> str | None:
        return sanitize_plain_text(value) if value is not None else None


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)

    @field_validator("full_name", mode="before")
    @classmethod
    def sanitize_full_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return sanitize_plain_text(v)

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return sanitize_plain_text(v)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Table User
class User(TimestampMixin, UserBase, SQLModel, table=True):
    __tablename__: ClassVar[str] = "users"  # pyright: ignore[reportIncompatibleVariableOverride]
    __table_args__ = (
        Index("ix_user_email", "email", unique=True),
        Index("ix_users_created_at", "created_at"),
    )
    id: int | None = Field(default=None, primary_key=True)
    hashed_password: str = Field(nullable=False, max_length=255)
    avatar_url: str | None = Field(default=None)

    is_verified: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    last_login_at: datetime | None = Field(default=None)

    # Relationships
    settings: "UserSettings" = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )
    notes: list["Notes"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"foreign_keys": "[Notes.user_id]"},
    )
    folders: list["NoteFolders"] = Relationship(back_populates="user")
    tags: list["NoteTags"] = Relationship(back_populates="user")
    templates: list["NoteTemplates"] = Relationship(back_populates="user")
    documents: list["Document"] = Relationship(back_populates="user")
    chat_sessions: list["ChatSession"] = Relationship(back_populates="user")
    activity_logs: list["ActivityLogs"] = Relationship(back_populates="user")
    note_collaborations: list["NoteCollaborators"] = Relationship(back_populates="user")
    google_drive_connection: Optional["GoogleDriveConnection"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )
    oauth_states: list["OAuthState"] = Relationship(
        back_populates="user", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    workspace_backups: list["WorkspaceBackup"] = Relationship(
        back_populates="user", sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    backup_schedule: Optional["BackupSchedule"] = Relationship(
        back_populates="user",
        sa_relationship_kwargs={"uselist": False, "cascade": "all, delete-orphan"},
    )


class EmailVerificationCode(SQLModel, table=True):
    __tablename__: ClassVar[str] = "email_verification_codes"  # pyright: ignore
    __table_args__ = (
        Index("ix_email_verification_expires_at", "expires_at"),
        Index("ix_email_verification_resend_available_at", "resend_available_at"),
    )

    user_id: int = Field(primary_key=True, foreign_key="users.id", ondelete="CASCADE")
    code_hash: str = Field(max_length=64, nullable=False)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    failed_attempts: int = Field(default=0, nullable=False)
    resend_available_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    window_started_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    send_count: int = Field(default=1, nullable=False)
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class UserPublic(UserBase):
    id: int
    is_verified: bool
    created_at: datetime | None = None


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


class UserTheme(StrEnum):
    light = "light"
    dark = "dark"
    auto = "auto"


class NotesViewMode(StrEnum):
    grid = "grid"
    list = "list"


class LlmProvider(StrEnum):
    openai = "openai"
    anthropic = "anthropic"
    ollama = "ollama"
    gemini = "gemini"
    huggingface = "huggingface"
    custom = "custom"


class UserSettings(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "user_settings"  # pyright: ignore
    __table_args__ = (
        CheckConstraint("chunk_size >= 100 AND chunk_size <= 4000", name="chk_chunk_size"),
        CheckConstraint("chunk_overlap >= 0 AND chunk_overlap <= 1000", name="chk_chunk_overlap"),
        CheckConstraint("top_k_results >= 1 AND top_k_results <= 20", name="chk_top_k_results"),
        CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="chk_similarity_threshold",
        ),
        CheckConstraint("temperature >= 0 AND temperature <= 1", name="chk_temperature"),
        CheckConstraint("max_tokens >= 100 AND max_tokens <= 4000", name="chk_max_tokens"),
    )
    user_id: int | None = Field(primary_key=True, foreign_key="users.id")
    llm_provider: LlmProvider = Field(default=LlmProvider.ollama)
    llm_model: str = Field(default="llama3.2:1b", max_length=100)
    embedding_model: str = Field(default="nomic-embed-text", max_length=100)
    chunk_size: int = Field(default=1000)
    chunk_overlap: int = Field(default=200)
    top_k_results: int = Field(default=5)
    similarity_threshold: float = Field(default=0.7)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=1000)
    theme: UserTheme = Field(default=UserTheme.light)
    language: str = Field(default="en", max_length=10)
    notes_view_mode: NotesViewMode = Field(default=NotesViewMode.grid)
    default_note_folder_id: int | None = Field(default=None, foreign_key="note_folders.id")
    email_notifications: bool = Field(default=True)
    processing_notifications: bool = Field(default=True)
    rag_diagnostics_enabled: bool = Field(default=False)

    # Relationships
    user: User = Relationship(back_populates="settings")
    default_folder: Optional["NoteFolders"] = Relationship(
        back_populates="user_settings",
        sa_relationship_kwargs={"foreign_keys": "[UserSettings.default_note_folder_id]"},
    )


class EntityType(StrEnum):
    note = "note"
    document = "document"
    chat = "chat"


class ActivityAction(StrEnum):
    created = "created"
    updated = "updated"
    deleted = "deleted"
    viewed = "viewed"
    shared = "shared"


class ActivityLogs(SQLModel, table=True):
    __tablename__: ClassVar[str] = "activitylogs"  # pyright: ignore
    __table_args__ = (
        Index("ix_activity_logs_user_created", "user_id", desc("created_at")),
        Index("ix_activity_logs_entity", "entity_type", "entity_id"),
    )

    id: int | None = Field(primary_key=True, default=None)
    user_id: int | None = Field(foreign_key="users.id", nullable=False)
    action: ActivityAction = Field(nullable=False)
    entity_type: EntityType = Field(nullable=False, max_length=50)
    entity_id: int | None = Field(nullable=False)
    details: dict | None = Field(default=None, sa_column=Column(JSONB))
    ip_address: str | None = Field(default=None)  # Store as string for INET type
    user_agent: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)

    # Relationships
    user: User = Relationship(back_populates="activity_logs")


class RefreshToken(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "refresh_tokens"  # pyright: ignore
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_hashed_token", "hashed_token", unique=True),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", nullable=False)
    hashed_token: str = Field(nullable=False, max_length=64)
    expires_at: datetime = Field(nullable=False)
    revoked: bool = Field(default=False)

    user: User = Relationship()


class TokenBlacklist(SQLModel, table=True):
    __tablename__: ClassVar[str] = "token_blacklist"  # pyright: ignore
    __table_args__ = (
        Index("ix_token_blacklist_jti", "jti", unique=True),
        Index("ix_token_blacklist_expires_at", "expires_at"),
    )
    id: int | None = Field(default=None, primary_key=True)
    jti: str = Field(nullable=False, max_length=36)
    expires_at: datetime = Field(nullable=False)
    blacklisted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Message(SQLModel):
    message: str


class Token(SQLModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenPayload(SQLModel):
    sub: str | None = None
    jti: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)
