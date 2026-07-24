from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Optional
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, String, desc
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import (
    CheckConstraint,
    Column,
    Field,
    Index,
    PrimaryKeyConstraint,
    Relationship,
    SQLModel,
    UniqueConstraint,
    text,
    true,
)

from .chat import TimestampMixin
from .portable import PortableIdMixin

if TYPE_CHECKING:
    from .chat import ChatSession
    from .document import Document
    from .user import User, UserSettings


class NoteFolders(PortableIdMixin, TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_folders"  # pyright: ignore
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "name",
            "parent_folder_id",
            name="uix_note_folders_user_name_parent_folder_id",
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int | None = Field(foreign_key="users.id", nullable=False, index=True)
    name: str = Field(max_length=255)
    description: str | None = Field(default=None)
    parent_folder_id: int | None = Field(default=None, foreign_key="note_folders.id", index=True)
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
            "foreign_keys": "[NoteFolders.parent_folder_id]",
        },
    )

    subfolders: list["NoteFolders"] = Relationship(
        back_populates="parent_folder",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    notes: list["Notes"] = Relationship(back_populates="folder")
    user_settings: list["UserSettings"] = Relationship(back_populates="default_folder")


class NoteTagRelations(SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_tag_relations"  # pyright: ignore
    __table_args__ = (PrimaryKeyConstraint("note_id", "tag_id"),)
    note_id: int = Field(foreign_key="notes.id", primary_key=True, index=True)
    tag_id: int = Field(foreign_key="note_tags.id", primary_key=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Notes(PortableIdMixin, TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "notes"  # pyright: ignore[reportIncompatibleVariableOverride]
    __table_args__ = (
        Index(
            "ix_notes_favorite",
            "user_id",
            "updated_at",
            desc("updated_at"),
            postgresql_where=true(),
        ),
        Index(
            "ix_notes_archived",
            "user_id",
            "updated_at",
            desc("updated_at"),
            postgresql_where=true(),
        ),
        Index(
            "ix_notes_search",
            text("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"),
            postgresql_using="gin",
        ),
        Index(
            "ix_notes_full_search",
            text(
                "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, '') || ' ' || coalesce(summary, ''))"
            ),
            postgresql_using="gin",
        ),
        Index(
            "ix_notes_title_trgm",
            text("lower(title) gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index(
            "ix_notes_content_trgm",
            text("lower(content) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )
    id: int | None = Field(default=None, primary_key=True)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int | None = Field(foreign_key="users.id", nullable=False)
    folder_id: int | None = Field(default=None, foreign_key="note_folders.id", index=True)
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
    linked_document_id: int | None = Field(default=None, foreign_key="documents.id", index=True)
    linked_chat_session_id: int | None = Field(
        default=None, foreign_key="chat_sessions.id", index=True
    )
    parent_note_id: int | None = Field(default=None, foreign_key="notes.id")
    version: int = Field(default=1)
    previous_version_id: int | None = Field(default=None, foreign_key="notes.id")
    is_public: bool = Field(default=False)
    is_locked: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    locked_by: int | None = Field(default=None, foreign_key="users.id")
    locked_at: datetime | None = Field(default=None)
    word_count: int | None = Field(default=None)
    char_count: int | None = Field(default=None)
    read_time_minutes: int | None = Field(default=None)
    last_accessed_at: datetime = Field(default_factory=datetime.now)
    last_edited_at: datetime = Field(default_factory=datetime.now)

    # Relationships
    user: "User" = Relationship(
        back_populates="notes",
        sa_relationship_kwargs={"foreign_keys": "[Notes.user_id]"},
    )
    folder: NoteFolders | None = Relationship(back_populates="notes")
    linked_document: Optional["Document"] | None = Relationship(
        back_populates="linked_notes",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_document_id]"},
    )
    linked_chat_session: Optional["ChatSession"] = Relationship(
        back_populates="linked_notes",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_chat_session_id]"},
    )
    parent_note: Optional["Notes"] | None = Relationship(
        back_populates="child_notes",
        sa_relationship_kwargs={
            "remote_side": "[Notes.id]",
            "foreign_keys": "[Notes.parent_note_id]",
        },
    )
    child_notes: list["Notes"] = Relationship(
        back_populates="parent_note",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "foreign_keys": "[Notes.parent_note_id]",
        },
    )
    previous_version: Optional["Notes"] | None = Relationship(
        sa_relationship_kwargs={
            "remote_side": "[Notes.id]",
            "foreign_keys": "[Notes.previous_version_id]",
        }
    )
    locked_by_user: Optional["User"] | None = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[Notes.locked_by]"}
    )
    tags: list["NoteTags"] = Relationship(back_populates="notes", link_model=NoteTagRelations)
    collaborators: list["NoteCollaborators"] = Relationship(back_populates="note")
    source_links: list["NoteLinks"] = Relationship(
        back_populates="source_note",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.source_note_id]"},
    )
    target_links: list["NoteLinks"] = Relationship(
        back_populates="target_note",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.target_note_id]"},
    )


class NoteTags(PortableIdMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_tags"  # pyright: ignore
    __table_args__ = (
        Index("ix_note_tags_user_name", "user_id", "name", unique=True),
        UniqueConstraint("user_id", "name", name="uix_note_tags_user_name"),
    )
    id: int = Field(primary_key=True, default=None)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int = Field(foreign_key="users.id", nullable=False)
    name: str = Field(nullable=False, max_length=100)
    color: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    user: "User" = Relationship(back_populates="tags")
    notes: list["Notes"] = Relationship(back_populates="tags", link_model=NoteTagRelations)


class NoteCategory(StrEnum):
    personal = "personal"
    meeting = "meeting"
    work = "work"
    study = "study"
    research = "research"
    other = "other"


class NoteTemplates(PortableIdMixin, TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_templates"  # pyright: ignore
    id: int | None = Field(primary_key=True, default=None)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int | None = Field(default=None, foreign_key="users.id")
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


class NoteCollaboratorsPermission(StrEnum):
    view = "view"
    edit = "edit"
    admin = "admin"
    comment = "comment"


class NoteCollaborators(SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_collaborators"  # pyright: ignore
    __table_args__ = (UniqueConstraint("note_id", "user_id", name="unique_note_collaborators"),)
    id: int | None = Field(primary_key=True, default=None)
    note_id: int | None = Field(foreign_key="notes.id", nullable=False)
    user_id: int | None = Field(foreign_key="users.id", nullable=False)
    permission: NoteCollaboratorsPermission = Field(default=NoteCollaboratorsPermission.view)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    accepted_at: datetime | None = Field(default=None)

    # Relationships
    note: Notes = Relationship(back_populates="collaborators")
    user: "User" = Relationship(back_populates="note_collaborations")


class NoteLinkType(StrEnum):
    related = "related"
    referenced = "referenced"
    parent = "parent"
    child = "child"


class NoteLinks(PortableIdMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "note_links"  # pyright: ignore
    __table_args__ = (
        CheckConstraint("source_note_id != target_note_id", name="check_note_links"),
        UniqueConstraint("source_note_id", "target_note_id", name="unique_note_links"),
    )
    id: int | None = Field(primary_key=True, default=None)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    source_note_id: int | None = Field(foreign_key="notes.id", nullable=False)
    target_note_id: int | None = Field(foreign_key="notes.id", nullable=False)
    link_type: NoteLinkType = Field(default=NoteLinkType.related)
    description: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Relationships
    source_note: Notes = Relationship(
        back_populates="source_links",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.source_note_id]"},
    )
    target_note: Notes = Relationship(
        back_populates="target_links",
        sa_relationship_kwargs={"foreign_keys": "[NoteLinks.target_note_id]"},
    )
