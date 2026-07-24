import os
import secrets
import warnings
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import AnyUrl, BeforeValidator, EmailStr, PostgresDsn, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",") if i.strip()]
    elif isinstance(v, list | str):
        return v
    raise ValueError


class Settings(BaseSettings):
    # Pydantic v2 configuration
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent.parent / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Security
    API_V1_STR: str = "/api/v1"
    # SECRET_KEY must be set via environment variable for persistent sessions
    # In production, this is REQUIRED. In local dev, a temporary key is generated if not set.
    SECRET_KEY: str = ""  # Will be set via env var or generated in local mode
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30  # 30 minutes
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 7 days
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    ACCESS_TOKEN_COOKIE_NAME: str = "auth-access-token"
    REFRESH_TOKEN_COOKIE_NAME: str = "auth-refresh-token"
    CSRF_COOKIE_NAME: str = "csrf-token"
    CSRF_HEADER_NAME: str = "X-CSRF-Token"

    # CORS Info
    FRONTEND_HOST: str = "http://localhost:8080"
    BACKEND_CORS_ORIGINS: Annotated[list[AnyUrl] | str, BeforeValidator(parse_cors)] = []

    @computed_field
    @property
    def all_cors_origins(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS] + [
            self.FRONTEND_HOST
        ]

    # Project Info
    PROJECT_NAME: str = "Cognolith"
    PROJECT_DESCRIPTION: str = "A connected workspace for knowledge, memory, and retrieval"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Server Configuration
    HOST: str = "localhost"
    PORT: int = 8000

    # Database Info
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "changethis"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "knowledge_assistant"

    @computed_field(alias="SQLMODEL_DATABASE_URL")
    @property
    def sqlmodel_database_url(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+psycopg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )

    DATABASE_URL: str | None = None

    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    @model_validator(mode="after")
    def _set_default_emails_from(self) -> Self:
        if not self.EMAILS_FROM_NAME:
            self.EMAILS_FROM_NAME = self.PROJECT_NAME
        return self

    @model_validator(mode="after")
    def _validate_secret_key(self) -> Self:
        """Ensure SECRET_KEY is set in production, generate ephemeral key in local mode."""
        if not self.SECRET_KEY:
            if self.ENVIRONMENT == "production":
                raise ValueError(
                    "SECRET_KEY environment variable must be set in production. "
                    'Generate a secure key with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
                )
            # Local development: generate ephemeral key (sessions won't persist across restarts)
            self.SECRET_KEY = secrets.token_urlsafe(32)
            warnings.warn(
                "SECRET_KEY not set. Using ephemeral key - sessions will NOT persist across restarts. "
                "Set SECRET_KEY environment variable for persistent sessions.",
                stacklevel=1,
            )
        return self

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48

    @computed_field
    @property
    def emails_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    @model_validator(mode="after")
    def _require_production_email(self) -> Self:
        if self.ENVIRONMENT == "production" and not self.emails_enabled:
            raise ValueError(
                "SMTP_HOST and EMAILS_FROM_EMAIL are required in production for email verification"
            )
        return self

    EMAIL_TEST_USER: str = "test@example.com"
    FIRST_SUPERUSER: str = "admin@example.com"
    FIRST_SUPERUSER_PASSWORD: str = "changethis"

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        placeholders = {
            "changethis",
            "change-me",
            "change-me-in-production",
            "postgres",
            "password",
            "secret",
        }
        if value and value.strip().lower() in placeholders:
            message = (
                f"The value of {var_name} is a known placeholder; "
                "replace it with a generated secret before deployment."
            )
            if self.ENVIRONMENT == "local":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)

        # Enforce strong passwords in production
        if self.ENVIRONMENT == "production":
            self._check_default_secret("POSTGRES_PASSWORD", self.POSTGRES_PASSWORD)
            self._check_default_secret("FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD)
        else:
            # Warn in local/staging
            self._check_default_secret("POSTGRES_PASSWORD", self.POSTGRES_PASSWORD)
            self._check_default_secret("FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD)
        return self

    # File Storage
    UPLOAD_DIR: Path = Path("./uploads")
    MAX_FILE_SIZE: int = 1024 * 1024 * 10  # 10 MB
    UPLOAD_CHUNK_SIZE: int = 64 * 1024
    MAX_DOCX_ENTRIES: int = 2_000
    MAX_DOCX_UNCOMPRESSED_SIZE: int = 50 * 1024 * 1024
    MAX_DOCX_ENTRY_SIZE: int = 20 * 1024 * 1024
    MAX_DOCX_COMPRESSION_RATIO: int = 100
    MAX_REQUEST_BODY_SIZE: int = (
        1024 * 1024 * 15
    )  # 15 MB (slightly larger than max file to allow form overhead)
    ALLOWED_EXTENSIONS: list[str] = [".pdf", ".md", ".docx", ".txt"]
    WEBSOCKET_MAX_MESSAGE_SIZE: int = 64 * 1024
    WEBSOCKET_MAX_CONTENT_LENGTH: int = 10_000

    # Google Drive backups
    GOOGLE_DRIVE_CLIENT_ID: str | None = None
    GOOGLE_DRIVE_CLIENT_SECRET: str | None = None
    GOOGLE_DRIVE_REDIRECT_URI: str = "http://localhost:3000/api/v1/users/me/google-drive/callback"
    GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY: str | None = None
    BACKUP_INTERVAL_HOURS: int = 24
    BACKUP_RETENTION_COUNT: int = 5
    BACKUP_MAX_ARCHIVE_SIZE: int = 1_073_741_824
    BACKUP_TEMP_DIR: Path = Path("./backup-temp")

    @computed_field
    @property
    def google_drive_backup_enabled(self) -> bool:
        return bool(
            self.GOOGLE_DRIVE_CLIENT_ID
            and self.GOOGLE_DRIVE_CLIENT_SECRET
            and self.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY
        )

    @model_validator(mode="after")
    def _validate_google_drive_backup_configuration(self) -> Self:
        configured_values = (
            self.GOOGLE_DRIVE_CLIENT_ID,
            self.GOOGLE_DRIVE_CLIENT_SECRET,
            self.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY,
        )
        if self.ENVIRONMENT == "production" and any(configured_values) and not all(configured_values):
            raise ValueError("Google Drive backup configuration is incomplete in production")
        return self

    # Database SSL
    # SSL modes: disable, allow, prefer, require, verify-ca, verify-full
    # Production MUST use: require, verify-ca, or verify-full
    DATABASE_SSL_MODE: str = "prefer"  # Allow unencrypted for local development

    @model_validator(mode="after")
    def _enforce_ssl_in_production(self) -> Self:
        """Enforce SSL for database connections in production environments."""
        insecure_modes = {"disable", "allow", "prefer"}
        if self.ENVIRONMENT == "production" and self.DATABASE_SSL_MODE in insecure_modes:
            raise ValueError(
                f"DATABASE_SSL_MODE must be 'require', 'verify-ca', or 'verify-full' in production. "
                f"Current value: '{self.DATABASE_SSL_MODE}'. "
                f"Update your environment configuration to use SSL."
            )
        return self

    # Rate Limiting
    RATE_LIMIT_WINDOW: int = 60  # seconds
    RATE_LIMIT_MAX_REQUESTS: int = 100
    SIGNUP_RATE_LIMIT: str = "5/hour"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_FILE: Path = Path("./logs/app.log")

    # AI
    # Ollama base URL - configurable via environment variable
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_CHAT_MODEL: str = "llama3.2:1b"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text"

    # Redis for distributed rate limiting (required in multi-worker deployments)
    # Set via REDIS_URL environment variable, e.g., redis://localhost:6379/0
    REDIS_URL: str | None = None

    def get_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        else:
            return f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"


settings = Settings()  # type: ignore

# Create necessary directories
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.LOG_FILE.parent, exist_ok=True)
