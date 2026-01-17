from uuid import uuid4
from sqlmodel import ARRAY, TIMESTAMP, UUID, Field, SQLModel
from sqlmodel.main import datetime


class NoteFolders(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE", unique=True)
    name: str = Field(default="", unique=True)
    description: str
    parent_folder_id: UUID = Field(foreign_key="note_folders.id", ondelete="CASCADE", unique=True)
    color: str
    icon: str
    emoji: str
    is_shared: bool = Field(default=False)
    is_archived: bool = Field(default= False)
    sort_order: int = Field(default=0)
    is_deleted: bool = Field(default=False)    
    created_at: TIMESTAMP = Field(default_factory=datetime.now)
    
class Notes(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user,id", ondelete="CASCADE")
    folder_id: UUID = Field(foreign_key="notes_folders.id", ondelete="SET NULL")
    title: str = Field(nullable=False)
    content: str = Field(nullable=False)
    content_type: str = Field(default="markdown")
    content_preview: str | None
    summary: str | None
    keywords: ARRAY = Field(default_factory=list)
    ai_generated: bool = Field(default=False)
    is_favourite: bool = Field(default=False)
    is_archived: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    color: str | None
    emoji: str | None
    linked_document_id: UUID = Field(foreign_key="documents.id", ondelete="SET NULL")
    linked_chat_session_id: UUID = Field(foreign_key="chat_sessions.id", ondelete="SET NULL")
    parent_note_id: UUID = Field(foreign_key="notes.id", ondelete="SET NULL")
    version: int = Field(default=1)
    previous_version_id: UUID = Field(foreign_key="notes.id",ondelete="SET NULL" )
    is_public: bool = Field(default=False)
    is_locked: bool = Field(default= False)
    locekd_by: UUID = Field(foreign_key="users.id", ondelete="SET NULL")
    locked_at: datetime
    word_count: int | None
    char_count: int | None
    read_time_minutes: int | None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_accessed_at: datetime = Field(default_factory=datetime.now)
    last_edited_at: datetime = Field(default_factory=datetime.now)
    
class NoteTags(SQLModel, table=True):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    user_id: UUID = Field(foreign_key="users.id", ondelete="CASCADE", unique=True)
    name: str = Field(nullable=False, unique=True)
    color: str | None
    description: str | None
    created_at: datetime = Field(default_factory=datetime.now)
    
class NoteTagRelations(SQLModel, table=True):
    note_id: UUID = Field(foreign_key="notes.id", ondelete="CASCADE")
    tag_id: UUID = Field(foreign_key="note_tags.id", ondelete="CASCADE")
    created_at: datetime = Field(default_factory=datetime.now)
    
class NoteTemplates(SQLModel, table=True):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    user_id: UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    name: str = Field(nullable=False)
    description: str | None
    category: str | None
    content: str = Field(nullable=False)
    content_type: str = Field(default="markdown")
    is_public: bool = Field(default=False)
    is_system: bool = Field(default=False)
    usage_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    
class NoteCollaborators(SQLModel, table=True):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    source_note_id: UUID = Field(foreign_key="notes.id", ondelete="CASCADE")
    target_note_id: UUID = Field(foreign_key="notes.id", ondelete="CASCADE")
    link_type: str = Field(default="related")
    description: str | None
    created_at: datetime = Field(default_factory=datetime.now)
