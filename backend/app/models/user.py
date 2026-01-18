from pydantic import EmailStr
from sqlmodel import JSON, Enum, SQLModel, Field, Relationship
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from chat import TimestampMixin

if TYPE_CHECKING:
    from .note import Notes, NoteFolders, NoteTags, NoteTemplates, NoteCollaborators
    from .document import Document
    from .chat import ChatSession

class User(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    email: EmailStr = Field(unique=True, nullable=False, index=True, max_length=255)
    full_name: str = Field(nullable=False, index=True)
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

class UserSettings(TimestampMixin, SQLModel, table=True):
    user_id: int | None = Field(primary_key=True, foreign_key="users.id", ondelete="CASCADE")
    llm_provider: str = Field(default="openai", max_length=50)
    llm_model: str = Field(default="gpt-3.5-turbo", max_length=100)
    embedding_model: str = Field(default="text-embedding-ada-002", max_length=100)
    chunk_size: int = Field(default=1000)
    chunk_overlap: int = Field(default=200)
    top_k_results: int = Field(default=5)
    similarity_threshold: float = Field(default=0.7)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=1000)
    theme: str = Field(default="light", max_length=20)
    language: str = Field(default="en", max_length=10)
    notes_view_mode: str = Field(default="grid", max_length=20)
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
    
class ActivityLogs(SQLModel, table=True):
    id: int | None = Field(primary_key=True, default=None)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    action: str = Field(nullable=False, max_length=50)
    entity_type: EntityType = Field(nullable=False, max_length=50)
    entity_id: int | None = Field(nullable=False)
    details: JSON | None = Field(default=None)
    ip_address: str | None = Field(default=None)  # Store as string for INET type
    user_agent: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    user: User = Relationship(back_populates="activity_logs")