"""
Tests the complete token lifecycle and security strategy:

1. Token Creation (JWT + Refresh pair)
2. Token Refresh (Rotation with revocation)
3. Token Revocation (Explicit + Logout)
4. Token Expiration & Cleanup
5. Token Validation (Blacklist checking)

Key Security Scenarios:
- JWT expiration (exp claim)
- Refresh token rotation (single-use enforcement)
- Token blacklisting (logout & revocation)
- TTL enforcement (access + refresh token)
- Session hijacking prevention
- Token replay attack prevention
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, col, delete, select

from app import crud
from app.core import security
from app.core.config import settings
from app.core.database import engine
from app.main import app
from app.models.backup import BackupSchedule, GoogleDriveConnection, WorkspaceBackup
from app.models.user import RefreshToken, TokenBlacklist, User, UserCreate
from app.services import auth_service

client = TestClient(app)


@pytest.fixture
def db_session():
    """Create a database session for tests."""
    with Session(engine) as session:
        # Clean up existing test data
        session.exec(delete(TokenBlacklist))
        session.exec(delete(RefreshToken))
        session.exec(delete(WorkspaceBackup))
        session.exec(delete(BackupSchedule))
        session.exec(delete(GoogleDriveConnection))
        session.exec(delete(User))
        session.commit()
        yield session


@pytest.fixture
def test_user(db_session: Session) -> User:
    """Create a test user."""
    user_create = UserCreate(
        email="tokentest@example.com",
        password="testpassword123",
        full_name="Token Test User",
    )
    return crud.create_user(session=db_session, user_create=user_create)


def _persisted_user_id(user: User) -> int:
    assert user.id is not None
    return user.id


class TestTokenCreation:
    """Test JWT + Refresh token pair creation."""

    def test_create_token_pair_returns_both_tokens(self, db_session: Session, test_user: User):
        """Token pair should contain both access and refresh tokens."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        assert token_pair.access_token is not None
        assert token_pair.refresh_token is not None
        assert token_pair.token_type == "bearer"
        assert len(token_pair.access_token) > 0
        assert len(token_pair.refresh_token) > 32  # Should be cryptographically secure

    def test_access_token_contains_exp_claim(self, db_session: Session, test_user: User):
        """Access token JWT should contain exp claim (expiration)."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        # Decode without verification to check claims
        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )

        assert "exp" in decoded
        assert "sub" in decoded
        assert "jti" in decoded
        assert decoded["sub"] == str(test_user.id)

        # Verify exp is 30 minutes in future
        exp_datetime = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        now = datetime.now(UTC)
        time_until_exp = (exp_datetime - now).total_seconds()

        # Should be approximately 30 minutes (allow 5 second drift)
        assert 1795 < time_until_exp < 1805

    def test_access_token_includes_jti(self, db_session: Session, test_user: User):
        """Access token should include JTI (JWT ID) for revocation tracking."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )

        assert "jti" in decoded
        assert len(decoded["jti"]) == 36  # UUID format

    def test_refresh_token_stored_in_database(self, db_session: Session, test_user: User):
        """Refresh token should be hashed and stored in database."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        # Check database for refresh token
        hashed = security.hash_refresh_token(token_pair.refresh_token)
        db_token = crud.get_refresh_token_by_hash(session=db_session, hashed_token=hashed)

        assert db_token is not None
        assert db_token.user_id == test_user.id
        assert db_token.revoked is False

    def test_refresh_token_expires_in_7_days(self, db_session: Session, test_user: User):
        """Refresh token should expire in 7 days."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        hashed = security.hash_refresh_token(token_pair.refresh_token)
        db_token = crud.get_refresh_token_by_hash(session=db_session, hashed_token=hashed)

        assert db_token is not None
        now = datetime.now(UTC)
        expires_at = db_token.expires_at.replace(tzinfo=UTC)
        time_until_exp = (expires_at - now).total_seconds()

        # Should be approximately 7 days (allow 5 min drift)
        expected_seconds = 7 * 24 * 60 * 60
        assert expected_seconds - 300 < time_until_exp < expected_seconds + 300

    def test_multiple_token_pairs_are_different(self, db_session: Session, test_user: User):
        """Each token pair should be unique."""
        token_pair1 = auth_service.create_token_pair(session=db_session, user=test_user)
        token_pair2 = auth_service.create_token_pair(session=db_session, user=test_user)

        # Tokens should be different
        assert token_pair1.access_token != token_pair2.access_token
        assert token_pair1.refresh_token != token_pair2.refresh_token

        # But should be for same user
        decoded1 = jwt.decode(
            token_pair1.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        decoded2 = jwt.decode(
            token_pair2.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        assert decoded1["sub"] == decoded2["sub"] == str(test_user.id)


class TestTokenRefresh:
    """Test token refresh and rotation."""

    def test_refresh_token_exchange_succeeds(self, db_session: Session, test_user: User):
        """Valid refresh token should exchange for new token pair."""
        initial_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        # Exchange refresh token for new pair
        new_pair = auth_service.refresh_access_token_from_refresh(
            session=db_session,
            user_id=_persisted_user_id(test_user),
            refresh_token=initial_pair.refresh_token,
        )

        assert new_pair.access_token != initial_pair.access_token
        assert new_pair.refresh_token != initial_pair.refresh_token

    def test_refresh_enforces_single_use(self, db_session: Session, test_user: User):
        """Refresh token should only work once (single-use enforcement)."""
        initial_pair = auth_service.create_token_pair(session=db_session, user=test_user)
        refresh_token = initial_pair.refresh_token

        # First refresh should succeed
        new_pair1 = auth_service.refresh_access_token_from_refresh(
            session=db_session,
            user_id=_persisted_user_id(test_user),
            refresh_token=refresh_token,
        )
        assert new_pair1 is not None

        # Second refresh with same token should fail
        with pytest.raises(ValueError, match="Invalid or expired refresh token"):
            auth_service.refresh_access_token_from_refresh(
                session=db_session,
                user_id=_persisted_user_id(test_user),
                refresh_token=refresh_token,  # Same token, already revoked
            )

    def test_refresh_revokes_old_token(self, db_session: Session, test_user: User):
        """Old refresh token should be revoked after refresh."""
        initial_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        # Refresh the token
        auth_service.refresh_access_token_from_refresh(
            session=db_session,
            user_id=_persisted_user_id(test_user),
            refresh_token=initial_pair.refresh_token,
        )

        # Old token should be revoked in database
        hashed_old = security.hash_refresh_token(initial_pair.refresh_token)

        # Query with revoked=True to find it
        from sqlmodel import and_

        statement = select(RefreshToken).where(
            and_(RefreshToken.hashed_token == hashed_old, col(RefreshToken.revoked).is_(True))
        )
        old_token = db_session.exec(statement).first()
        assert old_token is not None
        assert old_token.revoked is True

    def test_refresh_invalid_token_fails(self, db_session: Session, test_user: User):
        """Invalid refresh token should fail gracefully."""
        with pytest.raises(ValueError, match="Invalid or expired refresh token"):
            auth_service.refresh_access_token_from_refresh(
                session=db_session,
                user_id=_persisted_user_id(test_user),
                refresh_token="invalid_token_xyz",
            )

    def test_refresh_expired_token_fails(self, db_session: Session, test_user: User):
        """Expired refresh token should fail."""
        # Create a token manually with past expiration
        from app.core.security import hash_refresh_token

        raw_token = security.generate_refresh_token()
        expired_token = RefreshToken(
            user_id=_persisted_user_id(test_user),
            hashed_token=hash_refresh_token(raw_token),
            expires_at=datetime.now(UTC) - timedelta(days=1),
            revoked=False,
        )
        db_session.add(expired_token)
        db_session.commit()

        # Should fail because token is expired
        with pytest.raises(ValueError, match="Invalid or expired refresh token"):
            auth_service.refresh_access_token_from_refresh(
                session=db_session,
                user_id=_persisted_user_id(test_user),
                refresh_token=raw_token,
            )


class TestTokenRevocation:
    """Test token revocation and blacklisting."""

    def test_revoke_access_token_blacklists_jti(self, db_session: Session, test_user: User):
        """Revoking access token should add JTI to blacklist."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        jti = decoded["jti"]
        exp = decoded["exp"]

        # Revoke the token
        expires_at = datetime.fromtimestamp(exp, tz=UTC)
        auth_service.revoke_access_token(session=db_session, jti=jti, expires_at=expires_at)

        # Check blacklist
        assert auth_service.is_access_token_blacklisted(session=db_session, jti=jti)

    def test_blacklist_prevents_token_reuse(self, db_session: Session, test_user: User):
        """Blacklisted token JTI should be detected."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        jti = decoded["jti"]

        # Before revocation, should not be blacklisted
        assert not auth_service.is_access_token_blacklisted(session=db_session, jti=jti)

        # Revoke it
        expires_at = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        auth_service.revoke_access_token(session=db_session, jti=jti, expires_at=expires_at)

        # After revocation, should be blacklisted
        assert auth_service.is_access_token_blacklisted(session=db_session, jti=jti)

    def test_revoke_all_user_tokens_revokes_refresh_tokens(
        self, db_session: Session, test_user: User
    ):
        """Revoking all tokens should revoke all refresh tokens."""
        # Create multiple token pairs
        auth_service.create_token_pair(session=db_session, user=test_user)
        auth_service.create_token_pair(session=db_session, user=test_user)

        # Revoke all user tokens
        auth_service.revoke_all_user_tokens(
            session=db_session, user_id=_persisted_user_id(test_user)
        )

        # All refresh tokens should be revoked
        from sqlmodel import and_

        statement = select(RefreshToken).where(
            and_(
                RefreshToken.user_id == test_user.id,
                col(RefreshToken.revoked).is_(True),
            )
        )
        revoked_tokens = db_session.exec(statement).all()
        assert len(revoked_tokens) >= 2


class TestTokenExpiration:
    """Test token TTL (time-to-live) and automatic expiration."""

    def test_access_token_has_ttl_30_minutes(self, db_session: Session, test_user: User):
        """Access token should have 30-minute TTL by default."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )

        exp_datetime = datetime.fromtimestamp(decoded["exp"], tz=UTC)
        now = datetime.now(UTC)
        ttl_seconds = (exp_datetime - now).total_seconds()

        # Should be 30 minutes (1800 seconds), allow 5 second drift
        assert 1795 < ttl_seconds < 1805

    def test_refresh_token_has_ttl_7_days(self, db_session: Session, test_user: User):
        """Refresh token should have 7-day TTL by default."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        hashed = security.hash_refresh_token(token_pair.refresh_token)
        db_token = crud.get_refresh_token_by_hash(session=db_session, hashed_token=hashed)

        assert db_token is not None
        now = datetime.now(UTC)
        expires_at = db_token.expires_at.replace(tzinfo=UTC)
        ttl_seconds = (expires_at - now).total_seconds()

        # Should be 7 days (604800 seconds), allow 5 min drift
        expected = 7 * 24 * 60 * 60
        assert expected - 300 < ttl_seconds < expected + 300

    def test_cleanup_expired_tokens(self, db_session: Session, test_user: User):
        """Cleanup function should remove expired tokens."""
        # Create tokens with past expiration dates
        old_token = RefreshToken(
            user_id=_persisted_user_id(test_user),
            hashed_token=security.hash_refresh_token("old_token"),
            expires_at=datetime.now(UTC) - timedelta(days=1),
            revoked=True,
        )
        db_session.add(old_token)

        old_blacklist = TokenBlacklist(
            jti="old-jti", expires_at=datetime.now(UTC) - timedelta(days=1)
        )
        db_session.add(old_blacklist)
        db_session.commit()

        # Count tokens before cleanup
        statement = select(RefreshToken)
        count_before = len(db_session.exec(statement).all())
        assert count_before >= 1

        # Run cleanup
        deleted = auth_service.cleanup_expired_tokens(session=db_session)

        # Verify deleted
        assert deleted > 0

        # Count tokens after cleanup
        count_after = len(db_session.exec(statement).all())
        assert count_after < count_before


class TestTokenValidation:
    """Test token validation and security checks."""

    def test_validate_valid_access_token(self, db_session: Session, test_user: User):
        """Valid token should pass validation."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)

        decoded = jwt.decode(
            token_pair.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )

        # Should not be blacklisted
        jti = decoded.get("jti")
        assert isinstance(jti, str)
        assert not auth_service.is_access_token_blacklisted(session=db_session, jti=jti)

    def test_invalid_token_signature_fails(self, db_session: Session, test_user: User):
        """Token with invalid signature should fail verification."""
        # Create a token with wrong secret
        wrong_secret = "wrong_secret_key_that_is_long_enough"
        fake_token = jwt.encode(
            {
                "sub": str(test_user.id),
                "exp": datetime.now(UTC) + timedelta(minutes=30),
            },
            wrong_secret,
            algorithm=security.ALGORITHM,
        )

        # Should fail verification
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(fake_token, settings.SECRET_KEY, algorithms=[security.ALGORITHM])

    def test_expired_token_fails_verification(self, db_session: Session, test_user: User):
        """Expired token should fail verification."""
        # Create an expired token
        expired_payload = {
            "sub": str(test_user.id),
            "exp": datetime.now(UTC) - timedelta(hours=1),  # Past expiration
            "jti": "test-jti",
        }
        expired_token = jwt.encode(
            expired_payload, settings.SECRET_KEY, algorithm=security.ALGORITHM
        )

        # Should fail verification
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(expired_token, settings.SECRET_KEY, algorithms=[security.ALGORITHM])


class TestTokenIntegration:
    """Integration tests for complete token lifecycle."""

    def test_complete_login_refresh_logout_flow(self, db_session: Session, test_user: User):
        """Test complete token lifecycle: login → refresh → logout."""
        # 1. Login creates token pair
        token_pair1 = auth_service.create_token_pair(session=db_session, user=test_user)
        decoded1 = jwt.decode(
            token_pair1.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        jti1 = decoded1["jti"]

        # 2. Token should be valid
        assert not auth_service.is_access_token_blacklisted(session=db_session, jti=jti1)

        # 3. Refresh token to get new pair
        token_pair2 = auth_service.refresh_access_token_from_refresh(
            session=db_session,
            user_id=_persisted_user_id(test_user),
            refresh_token=token_pair1.refresh_token,
        )
        decoded2 = jwt.decode(
            token_pair2.access_token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
        )
        jti2 = decoded2["jti"]

        # Old token still valid (not blacklisted yet)
        assert not auth_service.is_access_token_blacklisted(session=db_session, jti=jti1)

        # 4. Logout revokes all tokens
        expires_at = datetime.fromtimestamp(decoded2["exp"], tz=UTC)
        auth_service.revoke_access_token(session=db_session, jti=jti2, expires_at=expires_at)
        auth_service.revoke_all_user_tokens(
            session=db_session, user_id=_persisted_user_id(test_user)
        )

        # New token should be blacklisted after logout
        assert auth_service.is_access_token_blacklisted(session=db_session, jti=jti2)

    def test_session_hijacking_prevention(self, db_session: Session, test_user: User):
        """Test that compromised refresh token doesn't allow infinite reuse."""
        token_pair = auth_service.create_token_pair(session=db_session, user=test_user)
        refresh_token = token_pair.refresh_token  # This could be compromised

        # First use succeeds
        new_pair1 = auth_service.refresh_access_token_from_refresh(
            session=db_session,
            user_id=_persisted_user_id(test_user),
            refresh_token=refresh_token,
        )
        assert new_pair1 is not None

        # Attacker tries to use same token again (without knowing new token)
        with pytest.raises(ValueError):
            auth_service.refresh_access_token_from_refresh(
                session=db_session,
                user_id=_persisted_user_id(test_user),
                refresh_token=refresh_token,  # Same token - should fail
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
