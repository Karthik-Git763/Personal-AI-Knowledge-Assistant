from uuid import uuid4
from sqlmodel import JSON, UUID, Field, SQLModel
from sqlmodel.main import datetime


class ChatSession(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    title: str
    description: str
    is_archived: bool = Field(default=False)
    is_pinned: bool = Field(default=False)
    created_at: datetime = Field(default=datetime.now)
    updated_at: datetime = Field(default=datetime.now)
    last_message_at: datetime = Field(default=datetime.now)
    
class ChatMessages(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    session_id: UUID = Field(foreign_key="chat_sessions.id", ondelete="CASCADE", nullable=False)
    role: str = Field(nullable=False)
    content: str = Field(nullable=False)
    sources: JSON | None
    model_used: str | None
    tokens_used: int | None
    response_time_ms: int | None
    rating: int | None
    feedback: str | None
    created_at: datetime = Field(default_factory=datetime.now)