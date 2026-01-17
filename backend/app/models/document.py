from sqlmodel import ARRAY, TIMESTAMP, Index, SQLModel, Field, Relationship
from uuid import uuid4, UUID

from sqlmodel.main import datetime

class Document(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", index=True)
    title: str = Field(nullable=False, index=True)
    file_name: str = Field(nullable=False, index=True)
    file_path: str = Field(nullable=False)
    file_size: int = Field(nullable=False)
    file_type: str = Field(nullable=False)
    mime_type: str = Field(nullable=False)
    content: str = Field(default="")
    content_preview: str = Field(default="")
    summary: ARRAY[str] = Field(default_factory=list)
    keywords: ARRAY[str] = Field(default_factory=list)
    tags: ARRAY[str] = Field(default_factory=list)
    language: str = Field(default="en", max_length=10)
    status: str = Field(default="processing", max_length=50)
    processing_started_at: TIMESTAMP = Field(default=None)
    processing_completed_at: TIMESTAMP = Field(default=None)
    processing_error: str = Field(default=None)
    word_count: int = Field(default=None)
    page_count: int = Field(default=None)
    chunk_count: int = Field(default=0)
    created_at: TIMESTAMP = Field(default=datetime.now, index=True)
    updated_at: TIMESTAMP = Field(default=datetime.now)
    last_accessed_at: TIMESTAMP = Field(default=None)


class DocumentChunks(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    document_id: UUID = Field(foreign_key="document.id", ondelete="CASCADE", unique=True)
    chunk_index: int = Field(default=0, unique=True)
    content: str = Field(default="")
    content_hash: str
    vector_id: str
    token_count: int
    char_count: int
    page_number: int
    section_title: str
    created_at: TIMESTAMP = Field(default=datetime.now)
