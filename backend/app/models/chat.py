from enum import Enum
from sqlmodel import Column, Field, SQLModel, Relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .user import User
    from .note import Notes

class TimestampMixin(SQLModel):
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ChatSession(TimestampMixin, SQLModel, table=True):    
    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(foreign_key="users.id", ondelete="CASCADE", nullable=False)
    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None)
    is_archived: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    last_message_at: datetime = Field(default_factory=datetime.now)
    
    # Relationships
    user: "User" = Relationship(back_populates="chat_sessions")
    messages: list["ChatMessages"] = Relationship(back_populates="session", sa_relationship_kwargs={
            "cascade": "all, delete-orphan",
            "order_by": "ChatMessages.created_at"
        })
    linked_notes: list["Notes"] = Relationship(
        back_populates="linked_chat_session",
        sa_relationship_kwargs={"foreign_keys": "[Notes.linked_chat_session_id]"}
    )

class ChatRole(str, Enum):
    user ="user"
    assistant = "asssistant"
    system = "system"

class ChatMessages(TimestampMixin, SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    session_id: int | None = Field(foreign_key="chat_sessions.id", ondelete="CASCADE", nullable=False)
    role: ChatRole = Field(nullable=False, max_length=20)
    content: str = Field(nullable=False)
    sources: dict | None = Field(default=None, sa_column=Column(JSONB))
    model_used: str | None = Field(default=None, max_length=100)
    tokens_used: int | None = Field(default=None)
    response_time_ms: int | None = Field(default=None)
    rating: int | None = Field(default=None)
    feedback: str | None = Field(default=None)
    
    # Relationships
    session: ChatSession = Relationship(back_populates="messages")