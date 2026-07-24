from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4

from pydantic import ConfigDict
from sqlalchemy import desc
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Column, Field, Index, Relationship, SQLModel, String

if TYPE_CHECKING:
    from .note import Notes
    from .user import User


class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"onupdate": lambda: datetime.now(UTC)},
    )


class ChatSession(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "chat_sessions"  # pyright: ignore
    __table_args__ = (
        Index("ix_chat_session_user_last_message", "user_id", desc("last_message_at")),
    )
    id: int | None = Field(default=None, primary_key=True)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    user_id: int | None = Field(default=None, foreign_key="users.id", nullable=False)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None)
    is_archived: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    last_message_at: datetime = Field(default_factory=datetime.now)

    # Relationships
    user: "User" = Relationship(back_populates="chat_sessions")
    messages: list["ChatMessages"] = Relationship(
        back_populates="session",
        sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "ChatMessages.created_at",
        },
    )
    linked_notes: list["Notes"] = Relationship(
        back_populates="linked_chat_session",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_chat_session_id]"},
    )


class ChatRole(StrEnum):
    user = "user"
    assistant = "assistant"
    system = "system"


class ChatGenerationStatus(StrEnum):
    streaming = "streaming"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class ChatMessages(TimestampMixin, SQLModel, table=True):
    __tablename__: ClassVar[str] = "chat_messages"  # pyright: ignore
    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", desc("created_at")),)
    model_config = ConfigDict(protected_namespaces=())  # pyright: ignore[reportAssignmentType]
    id: int | None = Field(default=None, primary_key=True)
    portable_id: UUID = Field(
        default_factory=uuid4,
        sa_column=Column(PGUUID(as_uuid=True), nullable=False, unique=True, index=True),
    )
    session_id: int | None = Field(foreign_key="chat_sessions.id", nullable=False)
    role: ChatRole = Field(nullable=False, max_length=20)
    content: str = Field(nullable=False)
    sources: dict | None = Field(default=None, sa_column=Column(JSONB))
    model_used: str | None = Field(default=None, max_length=100)
    tokens_used: int | None = Field(default=None)
    response_time_ms: int | None = Field(default=None)
    rating: int | None = Field(default=None)
    feedback: str | None = Field(default=None)
    generation_status: ChatGenerationStatus | None = Field(
        default=None, sa_column=Column(String(20), nullable=True)
    )
    generation_error: str | None = Field(default=None)
    generation_metadata: dict | None = Field(default=None, sa_column=Column(JSONB))
    generation_started_at: datetime | None = Field(default=None)
    generation_completed_at: datetime | None = Field(default=None)
    # Relationships
    session: ChatSession = Relationship(back_populates="messages")
