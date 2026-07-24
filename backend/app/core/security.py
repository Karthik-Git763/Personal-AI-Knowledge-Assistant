import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from app.core.config import settings

password_hash = PasswordHash(
    (
        Argon2Hasher(),
        BcryptHasher(),
    )
)
ALGORITHM = "HS256"


class ProviderTokenEncryptionError(RuntimeError):
    """Raised when Google Drive token encryption cannot be safely performed."""


def _provider_token_fernet() -> Fernet:
    key = settings.GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY
    if not key:
        raise ProviderTokenEncryptionError("Google Drive token encryption is not configured")
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as error:
        raise ProviderTokenEncryptionError("Google Drive token encryption configuration is invalid") from error


def encrypt_provider_token(value: str) -> bytes:
    """Encrypt a provider credential with the dedicated Google Drive key."""
    return _provider_token_fernet().encrypt(value.encode())


def decrypt_provider_token(value: bytes) -> str:
    """Decrypt a provider credential without exposing ciphertext failures."""
    try:
        return _provider_token_fernet().decrypt(value).decode()
    except (InvalidToken, UnicodeDecodeError) as error:
        raise ProviderTokenEncryptionError("Google Drive provider token cannot be decrypted") from error


def create_access_token(subject: str | Any, expires_delta: timedelta) -> str:
    expire = datetime.now(UTC) + expires_delta
    jti = str(uuid.uuid4())
    to_encode = {"exp": expire, "sub": str(subject), "jti": jti}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def generate_refresh_token() -> str:
    """Generate a cryptographically secure refresh token."""
    return secrets.token_urlsafe(64)


def hash_refresh_token(token: str) -> str:
    """Hash a refresh token for secure storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(plain_password: str, hashed_password: str) -> tuple[bool, str | None]:
    return password_hash.verify_and_update(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return password_hash.hash(password)
