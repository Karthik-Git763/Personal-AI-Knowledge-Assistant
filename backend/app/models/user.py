from pydantic import EmailStr
from sqlmodel import CheckConstraint, Column, Enum, Index, SQLModel, Field, Relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from chat import TimestampMixin

if TYPE_CHECKING:
    from .note import Notes, NoteFolders, NoteTags, NoteTemplates, NoteCollaborators
    from .document import Document
    from .chat import ChatSession

class User(TimestampMixin, SQLModel, table=True):
    __table_args__ = (
        Index("ix_user_email", "email", unique=True),
        Index("ix_users_created_at", "created_at")
    )
    id: int | None = Field(default=None, primary_key=True)
    email: EmailStr = Field(unique=True, nullable=False, index=True, max_length=255)
    full_name: str = Field(nullable=False)
    hashed_password: str = Field(nullable=False, max_length=255)
    avatar_url: str | None = Field(default=None)

    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)
    is_verified: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    last_login_at: datetime | None = Field(default=None)
    
    # Relationships
    settings: "UserSettings" = Relationship(
        back_populates="user", 
        sa_relationship_kwargs={
        "uselist": False,
        "cascade": "all, delete-orphan"
    })
    notes: list["Notes"] = Relationship(back_populates="user")
    folders: list["NoteFolders"] = Relationship(back_populates="user")
    tags: list["NoteTags"] = Relationship(back_populates="user")
    templates: list["NoteTemplates"] = Relationship(back_populates="user")
    documents: list["Document"] = Relationship(back_populates="user")
    chat_sessions: list["ChatSession"] = Relationship(back_populates="user")
    activity_logs: list["ActivityLogs"] = Relationship(back_populates="user")
    note_collaborations: list["NoteCollaborators"] = Relationship(back_populates="user")

class UserTheme(str, Enum):
    light = "light"
    dark = "dark"
    auto = "auto"

class NotesViewMode(str, Enum):
    grid = "grid"
    list = "list"

class LlmProvider(str, Enum):
    openai = "openai"
    anthropic = "anthropic"
    ollama = "ollama"
    gemini = "gemini"
    huggingface = "huggingface"
    custom = "custom"

class UserSettings(TimestampMixin, SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("chunk_size >= 100 AND chunk_size <= 4000", name="chk_chunk_size"),
        CheckConstraint("chunk_overlap >= 0 AND chunk_overlap <= 1000", name="chk_chunk_overlap"),
        CheckConstraint("top_k_results >= 1 AND top_k_results <= 20", name="chk_top_k_results"),
        CheckConstraint("similarity_threshold >= 0 AND similarity_threshold <= 1", name="chk_similarity_threshold"),
        CheckConstraint("temperature >= 0 AND temperature <= 1", name="chk_temperature"),
        CheckConstraint("max_tokens >= 100 AND max_tokens <= 4000", name="chk_max_tokens"),
    )
    user_id: int | None = Field(primary_key=True, foreign_key="users.id", ondelete="CASCADE")
    llm_provider: LlmProvider = Field(default=LlmProvider.ollama)
    llm_model: str = Field(default="tinyllama", max_length=100)
    embedding_model: str = Field(default="text-embedding-ada-002", max_length=100)
    chunk_size: int = Field(default=1000)
    chunk_overlap: int = Field(default=200)
    top_k_results: int = Field(default=5)
    similarity_threshold: float = Field(default=0.7)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=1000)
    theme: UserTheme = Field(default=UserTheme.light)
    language: str = Field(default="en", max_length=10)
    notes_view_mode: NotesViewMode = Field(default=NotesViewMode.grid)
    default_note_folder_id: int | None = Field(default=None, foreign_key="note_folders.id", ondelete="SET NULL")
    email_notifications: bool = Field(default=True)
    processing_notifications: bool = Field(default=True)
    
    # Relationships
    user: User = Relationship(back_populates="settings")
    default_folder: Optional["NoteFolders"] = Relationship(
        sa_relationship_kwargs={
            "foreign_keys": "[UserSettings.default_note_folder_id]",
            "cascade": "all, delete-orphan"
        }
    )
    
class EntityType(str, Enum):
    note = "note"
    document = "document"
    chat = "chat"

class ActivityAction(str, Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"
    viewed = "viewed"
    shared = "shared"

class ActivityLogs(SQLModel, table=True):
    __table_args__ = (
        Index("ix_activity_logs_user_created", "user_id", "created_at", postgresql_sort={"created_at": "DESC"}),
        Index("ix_activity_logs_entity", "entity_type", "entity_id")
    )
    
    id: int | None = Field(primary_key=True, default=None)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    action: ActivityAction = Field(nullable=False)
    entity_type: EntityType = Field(nullable=False, max_length=50)
    entity_id: int | None = Field(nullable=False)
    details: dict | None = Field(default=None, sa_column=Column(JSONB))
    ip_address: str | None = Field(default=None)  # Store as string for INET type
    user_agent: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    user: User = Relationship(back_populates="activity_logs")