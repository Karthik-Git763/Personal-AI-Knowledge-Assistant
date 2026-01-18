from sqlmodel import CheckConstraint, Enum, Field, Index, PrimaryKeyConstraint, SQLModel, Column, Relationship, UniqueConstraint, text, true
from sqlalchemy import ARRAY, String
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional
from chat import TimestampMixin

if TYPE_CHECKING:
    from .user import User, UserSettings
    from .document import Document
    from .chat import ChatSession


class NoteFolders(TimestampMixin, SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "name", "parent_folder_id", name="uix_note_folders_user_name_parent_folder_id")
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False, index=True)
    name: str = Field(max_length=255)
    description: str | None = Field(default=None)
    parent_folder_id: int | None = Field(default=None, foreign_key="note_folders.id", ondelete="CASCADE", index=True)
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)
    emoji: str | None = Field(default=None, max_length=10)
    is_shared: bool = Field(default=False)
    is_archived: bool = Field(default=False)
    sort_order: int = Field(default=0)
    is_deleted: bool = Field(default=False)
    
    # Relationships
    user: "User" = Relationship(back_populates="folders")
    parent_folder: Optional["NoteFolders"] = Relationship(
            back_populates="subfolders",
            sa_relationship_kwargs={
                "remote_side": "NoteFolders.id",
                "foreign_keys": "[NoteFolders.parent_folder_id]"
            }
        )
    
    subfolders: List["NoteFolders"] = Relationship(
        back_populates="parent_folder",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    notes: List["Notes"] = Relationship(back_populates="folder")
    user_settings: List["UserSettings"] = Relationship()

class NoteTagRelations(SQLModel, table=True):
    __table_args__ = (
        PrimaryKeyConstraint("note_id", "tag_id")
    )
    note_id: int = Field(foreign_key="notes.id", ondelete="CASCADE", primary_key=True, index=True)
    tag_id: int = Field(foreign_key="note_tags.id", ondelete="CASCADE", primary_key=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class Notes(TimestampMixin, SQLModel, table=True):
    __table_args__ = (
        Index("ix_notes_favorite", "user_id", "updated_at", postgresql_sort={"updated_at": "DESC"}  , postgresql_where=true()),
        Index("ix_notes_archived", "user_id", "updated_at", postgresql_sort={"updated_at": "DESC"}  , postgresql_where=true()),
        Index("ix_notes_search", text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"), postgresql_using="gin"),
        Index("ix_notes_full_search", text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, '') || ' ' || coalesce(summary, ''))"), postgresql_using="gin")
    )
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    folder_id: int | None = Field(default=None, foreign_key="note_folders.id", ondelete="SET NULL", index=True)
    title: str = Field(nullable=False, max_length=500)
    content: str = Field(nullable=False)
    content_type: str = Field(default="markdown", max_length=20)
    content_preview: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None)
    keywords: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String)))
    ai_generated: bool = Field(default=False)
    is_favorite: bool = Field(default=False)
    is_archived: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    color: str | None = Field(default=None, max_length=20)
    emoji: str | None = Field(default=None, max_length=10)
    linked_document_id: int | None = Field(default=None, foreign_key="documents.id", ondelete="SET NULL", index=True)
    linked_chat_session_id: int | None = Field(default=None, foreign_key="chat_sessions.id", ondelete="SET NULL", index=True)
    parent_note_id: int | None = Field(default=None, foreign_key="notes.id", ondelete="SET NULL")
    version: int = Field(default=1)
    previous_version_id: int | None = Field(default=None, foreign_key="notes.id", ondelete="SET NULL")
    is_public: bool = Field(default=False)
    is_locked: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    locked_by: int | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL")
    locked_at: datetime | None = Field(default=None)
    word_count: int | None = Field(default=None)
    char_count: int | None = Field(default=None)
    read_time_minutes: int | None = Field(default=None)
    last_accessed_at: datetime = Field(default_factory=datetime.now)
    last_edited_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    user: "User" = Relationship(back_populates="notes")
    folder: NoteFolders | None = Relationship(back_populates="notes")
    linked_document: Optional["Document"] | None = Relationship(
        back_populates="linked_notes", 
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_document_id]"}
    )
    linked_chat_session: Optional["ChatSession"] = Relationship(
        back_populates="linked_notes",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_chat_session_id]"}
    )
    parent_note: Optional["Notes"] | None = Relationship(
        back_populates="child_notes",
        sa_relationship_kwargs={"remote_side": "[Notes.id]", "foreign_keys": "[Notes.parent_note_id]"}
    )
    child_notes: list["Notes"] = Relationship(
        back_populates="parent_note",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
    previous_version: Optional["Notes"] | None = Relationship(
        sa_relationship_kwargs={"remote_side": "[Notes.id]", "foreign_keys": "[Notes.previous_version_id]"}
    )
    locked_by_user: Optional["User"] | None = Relationship(sa_relationship_kwargs={"foreign_keys": "[Notes.locked_by]"})
    tags: list["NoteTags"] = Relationship(back_populates="notes", link_model=NoteTagRelations)
    collaborators: list["NoteCollaborators"] = Relationship(back_populates="note")
    source_links: list["NoteLinks"] = Relationship(
        back_populates="source_note",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.source_note_id]"}
    )
    target_links: list["NoteLinks"] = Relationship(
        back_populates="target_note",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.target_note_id]"}
    )

class NoteTags(SQLModel, table=True):
    __table_args__ = (
        Index("ix_note_tags_user_name", "user_id", "name", unique=True),
        UniqueConstraint("user_id", "name", name="uix_note_tags_user_name")
    )
    id: int = Field(primary_key=True, default=None)
    user_id: int = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    name: str = Field(nullable=False, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Relationships
    user: "User" = Relationship(back_populates="tags")
    notes: list["Notes"] = Relationship(back_populates="tags", link_model=NoteTagRelations)
    
class NoteCategory(str, Enum):
    personal = "personal"
    meeting = "meeting"
    work = "work"
    study = "study"
    research = "research"
    other = "other"

class NoteTemplates(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(primary_key=True, default=None)
    user_id: int | None = Field(default=None, foreign_key="users.id", ondelete="CASCADE")
    name: str = Field(nullable=False, max_length=255)
    description: str | None = Field(default=None)
    category: NoteCategory = Field(default=None)
    content: str = Field(nullable=False)
    content_type: str = Field(default="markdown", max_length=20)
    is_public: bool = Field(default=False)
    is_system: bool = Field(default=False)
    usage_count: int = Field(default=0)
    
    # Relationships
    user: Optional["User"] = Relationship(back_populates="templates")

class NoteCollaboratorsPermission(str, Enum):
    view = "view"
    edit = "edit"
    admin = "admin"
    comment = "comment"

class NoteCollaborators(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("note_id", "user_id", name="unique_note_collaborators")
    )
    id: int | None = Field(primary_key=True, default=None)
    note_id: int | None = Field(foreign_key="notes.id", ondelete="CASCADE", nullable=False)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    permission: NoteCollaboratorsPermission = Field(default=NoteCollaboratorsPermission.view)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    accepted_at: datetime | None = Field(default=None)
    
    # Relationships
    note: Notes = Relationship(back_populates="collaborators")
    user: "User" = Relationship(back_populates="note_collaborations")
    
class NoteLinkType(str, Enum):
    related = "related"
    referenced = "referenced"
    parent = "parent"
    child = "child"

class NoteLinks(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("source_note_id != target_note_id", name="check_note_links"),
        UniqueConstraint("source_note_id", "target_note_id", name="unique_note_links")
    )
    id: int | None = Field(primary_key=True, default=None)
    source_note_id: int | None = Field(foreign_key="notes.id", ondelete="CASCADE", nullable=False)
    target_note_id: int | None = Field(foreign_key="notes.id", ondelete="CASCADE", nullable=False)
    link_type: NoteLinkType = Field(default=NoteLinkType.related)
    description: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Relationships
    source_note: Notes = Relationship(
        back_populates="source_links",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.source_note_id]"}
    )
    target_note: Notes = Relationship(
        back_populates="target_links",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.target_note_id]"}
    )