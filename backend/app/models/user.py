from ipaddress import IPv4Address
from pydantic import EmailStr
from sqlmodel import JSON, SQLModel, Field
from uuid import UUID, uuid4
from datetime import datetime

class User(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: EmailStr = Field(unique=True, nullable=False, index=True)
    full_name: str = Field(nullable=False, index=True)
    hashed_password: str = Field(nullable=False)
    avatar_url: str = Field(default=None)
    
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)
    is_verified: bool = Field(default=False)
    is_deleted: bool = Field(default=False)
    
    created_at: datetime = Field(default_factory=datetime.now, index=True)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_login_at: datetime = Field(default_factory=datetime.now)
    
class UserSettings(SQLModel, table=True):
    user_id: UUID = Field(primary_key= True, foreign_key="users.id", ondelete="CASCADE")
    llm_provider: str = Field(default="ollama")
    llm_model: str = Field(default="tinyollama")
    embedding_model: str = Field(default="text-embedding-ada-002")
    chunk_size: int = Field(default=1000)
    chunk_overlap: int = Field(default=200)
    top_k_results: int = Field(default=5)
    similarity_threshold: float = Field(default=0.7)
    temperature: float = Field(default=0.7)
    max_tokens: int = Field(default=1000)
    theme: str = Field(default="light")
    language: str = Field(default="en", max_length=10)
    notes_view_mode: str = Field(default="grid")
    default_note_folder_id: UUID = Field(foreign_key="notes_folders.id", ondelete="SET NULL")
    email_notifications: bool = Field(default=True)
    processing_notifications: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    
class ActivityLogs(SQLModel, table=True):
    id: UUID = Field(primary_key=True, default_factory=uuid4)
    user_id: UUID = Field(foreign_key="users.id", ondelete="CASCADE")
    action: str = Field(nullable=False)
    entity_type: str = Field(nullable=False)
    entity_id: UUID = Field(nullable=False)
    details: JSON | None
    ip_address: IPv4Address | None
    user_agent: str | None
    created_at: datetime = Field(default_factory=datetime.now)