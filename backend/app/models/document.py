from sqlmodel import SQLModel, Field, Column, Relationship
from sqlalchemy import ARRAY, String
from datetime import datetime
from typing import TYPE_CHECKING
from chat import TimestampMixin

if TYPE_CHECKING:
    from .user import User
    from .note import Notes

class Document(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False, index=True)
    title: str = Field(nullable=False, index=True, max_length=255)
    file_name: str = Field(nullable=False, max_length=255)
    file_path: str = Field(nullable=False)
    file_size: int = Field(nullable=False)
    file_type: str = Field(nullable=False, max_length=255)
    mime_type: str = Field(nullable=False, max_length=255)
    is_deleted: bool = Field(default=False)
    content: str | None = Field(default=None)
    content_preview: str | None = Field(default=None)
    summary: str | None = Field(default=None)  # TEXT not ARRAY
    keywords: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String)))
    tags: list[str] = Field(default_factory=list, sa_column=Column(ARRAY(String)))
    language: str = Field(default="en", max_length=10)
    status: str = Field(default="processing", max_length=50)
    processing_started_at: datetime | None = Field(default=None)
    processing_completed_at: datetime | None = Field(default=None)
    processing_error: str | None = Field(default=None)
    word_count: int | None = Field(default=None)
    page_count: int | None = Field(default=None)
    chunk_count: int = Field(default=0)
    last_accessed_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    user: "User" = Relationship(back_populates="documents")
    chunks: list["DocumentChunks"] = Relationship(
        back_populates="document",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "DocumentChunks.chunk_index"
        }
    )
    linked_notes: list["Notes"] = Relationship(
        back_populates="linked_document",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_document_id]"}
    )


class DocumentChunks(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    document_id: int | None = Field(foreign_key="documents.id", ondelete="CASCADE", nullable=False)
    chunk_index: int | None = Field(nullable=False)
    content: str = Field(nullable=False)
    content_hash: str | None = Field(default=None, max_length=255)
    vector_id: str = Field(nullable=False)
    token_count: int | None = Field(default=None)
    char_count: int | None = Field(default=None)
    page_number: int | None = Field(default=None)
    section_title: str | None = Field(default=None, max_length=255)
    
    # Relationships
    document: Document = Relationship(back_populates="chunks")
