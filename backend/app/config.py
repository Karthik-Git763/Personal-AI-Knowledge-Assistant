from pydantic_settings import BaseSettings
from typing import List
import os
from pathlib import Path

class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "Personal Knowledge Assistant"
    PROJECT_DESCRIPTION: str = "A personal knowledge assistant"
    VERSION: str = "0.1.0"
    DEBUG: bool = True
    
    # Server Configuration
    HOST: str = "localhost"
    PORT: int = 8000
    
    # Database Info
    DATABASE_URL: str = "postgresql://user:password@localhost/dbname"

    # CORS Info
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
    ]

    # Security
    SECRET_KEY: str = "your_secret_key_here"
    
    # File Storage
    UPLOAD_DIR: Path = Path("./uploads")
    MAX_FILE_SIZE: int = 1024 * 1024 * 10  # 10 MB
    ALLOWED_EXTENSIONS: List[str] = [".pdf", ".md", ".docx", ".txt"]
    
    # Rate Limiting
    RATE_LIMIT_WINDOW: int = 60  # seconds
    RATE_LIMIT_MAX_REQUESTS: int = 100

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_FILE: Path = Path("./logs/app.log")
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        
settings = Settings()

# Create necessary directories
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.LOG_FILE.parent, exist_ok=True)
