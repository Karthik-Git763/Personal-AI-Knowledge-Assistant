import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.deps import get_current_user
from app.api.routes.backups import _adopt_remote_backups
from app.core.config import settings
from app.core.database import engine
from app.main import app
from app.models.backup import (
    BackupSchedule,
    BackupStatus,
    DriveConnectionStatus,
    GoogleDriveConnection,
    OAuthState,
    WorkspaceBackup,
)
from app.models.user import User
from app.services.backup_coordinator import BackupCoordinator
from app.services.backup_store import BackupObjectMetadata, StoredBackup
from app.services.google_drive_oauth import (
    GoogleDriveOAuthError,
    GoogleDriveOAuthService,
    derive_drive_owner_id,
)
from app.services.google_drive_store import GoogleDriveRetryableError


def _authenticated_client(session) -> tuple[TestClient, User]:
    user = User(
        email="backup-routes@example.com",
        hashed_password="hashed-password",
        is_verified=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    assert user.id is not None

    def current_user() -> User:
        return user

    app.dependency_overrides[get_current_user] = current_user
    client = TestClient(app)
    csrf_token = "a" * 32
    client.cookies.set("csrf-token", csrf_token)
    client.headers.update({"X-CSRF-Token": csrf_token})
    return client, user


def _configure_drive(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(settings, "GOOGLE_DRIVE_TOKEN_ENCRYPTION_KEY", "token-key")


def _connection(session, user: User) -> GoogleDriveConnection:
    assert user.id is not None
    connection = GoogleDriveConnection(
        user_id=user.id,
        encrypted_refresh_token=b"encrypted-token",
        google_subject="google-subject",
        google_email=user.email,
    )
    session.add(connection)
    session.commit()
    return connection


def _remote_backup(
    remote_id: str,
    *,
    owner_id: UUID,
    completed: bool = True,
    schema_version: int = 1,
    created_at: datetime | None = None,
    backup_id: UUID | None = None,
) -> StoredBackup:
    timestamp = created_at or datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    return StoredBackup(
        remote_id=remote_id,
        name=f"cognolith-{remote_id}.zip",
        size=100,
        created_at=timestamp,
        metadata=BackupObjectMetadata(
            drive_owner_id=owner_id,
            workspace_owner_id=uuid4(),
            backup_id=backup_id or uuid4(),
            schema_version=schema_version,
            archive_checksum="a" * 64,
            created_at=timestamp,
        ),
        completed=completed,
    )


def test_backup_routes_require_authentication() -> None:
    client = TestClient(app)

    assert client.get("/api/v1/users/me/backups").status_code == 401


def test_manual_backup_returns_pending_operation(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    _connection(session, user)
    _configure_drive(monkeypatch)

    async def run_backup(*_: object) -> None:
        return None

    monkeypatch.setattr(BackupCoordinator, "run_backup", run_backup)

    try:
        response = client.post("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["status"] == "pending"


def test_connect_returns_google_authorization_url(session, monkeypatch) -> None:
    client, _ = _authenticated_client(session)
    _configure_drive(monkeypatch)
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?state=state-value"
    monkeypatch.setattr(
        GoogleDriveOAuthService, "create_authorization_url", lambda *_: authorization_url
    )
    try:
        response = client.post("/api/v1/users/me/google-drive/connect")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"authorization_url": authorization_url}


def test_manual_backup_conflicts_when_an_operation_is_active(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    _connection(session, user)
    _configure_drive(monkeypatch)
    session.add(WorkspaceBackup(user_id=user.id, status=BackupStatus.pending))
    session.commit()

    try:
        response = client.post("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["error"] == "BACKUP_OPERATION_ACTIVE"


def test_backup_routes_apply_csrf_protection(session) -> None:
    client, _ = _authenticated_client(session)
    client.cookies.clear()
    client.headers.clear()

    try:
        response = client.post("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


def test_status_reports_unconfigured_deployment(session) -> None:
    client, _ = _authenticated_client(session)

    try:
        response = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"] == "GOOGLE_DRIVE_NOT_CONFIGURED"


def test_restore_requires_exact_confirmation(session) -> None:
    client, _ = _authenticated_client(session)

    try:
        response = client.post(
            f"/api/v1/users/me/backups/{uuid4()}/restore",
            json={"confirmation": "yes"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_operation_is_scoped_to_current_user(session) -> None:
    client, user = _authenticated_client(session)
    foreign = User(email="foreign@example.com", hashed_password="hashed-password", is_verified=True)
    session.add(foreign)
    session.commit()
    assert foreign.id is not None
    operation = WorkspaceBackup(user_id=foreign.id, status=BackupStatus.pending)
    session.add(operation)
    session.commit()

    try:
        response = client.get(f"/api/v1/users/me/backups/operations/{operation.backup_id}")
    finally:
        app.dependency_overrides.clear()

    assert user.id != foreign.id
    assert response.status_code == 404


def test_status_redacts_provider_credentials(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    _configure_drive(monkeypatch)

    async def no_backups(*_: object) -> list[object]:
        return []

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", no_backups)
    try:
        response = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "encrypted_refresh_token" not in response.text
    assert connection.google_subject not in response.text


def test_status_maps_retryable_drive_failures_to_safe_envelope(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    connection = _connection(session, user)
    _configure_drive(monkeypatch)

    class Store:
        async def list(self, _: UUID) -> list[StoredBackup]:
            raise GoogleDriveRetryableError("provider response must not leak")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("app.api.routes.backups._store", lambda *_: Store())
    try:
        response = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["error"] == "GOOGLE_DRIVE_RETRYABLE"
    assert "provider response" not in response.text
    assert connection.google_subject not in response.text


def test_overdue_status_requests_collapse_to_one_operation(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    _connection(session, user)
    _configure_drive(monkeypatch)
    schedule = BackupSchedule(
        user_id=user.id,
        enabled=True,
        next_due_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.add(schedule)
    session.commit()

    async def no_backups(*_: object) -> list[object]:
        return []

    async def run_backup(*_: object) -> None:
        return None

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", no_backups)
    monkeypatch.setattr(BackupCoordinator, "run_backup", run_backup)
    try:
        first = client.get("/api/v1/users/me/google-drive/status")
        second = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    operations = session.exec(
        select(WorkspaceBackup).where(WorkspaceBackup.user_id == user.id)
    ).all()
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(operations) == 1


def test_callback_uses_state_binding_and_rejects_replay(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    _configure_drive(monkeypatch)
    raw_state = "state-value"
    session.add(
        OAuthState(
            user_id=user.id,
            state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    session.commit()

    async def complete_authorization(self, user_id: int, state: str, code: str):
        self.consume_state(user_id, state)
        return object()

    monkeypatch.setattr(GoogleDriveOAuthService, "complete_authorization", complete_authorization)
    try:
        first = client.get(
            "/api/v1/users/me/google-drive/callback",
            params={"state": raw_state, "code": "authorization-code"},
            follow_redirects=False,
        )
        replay = client.get(
            "/api/v1/users/me/google-drive/callback",
            params={"state": raw_state, "code": "authorization-code"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert first.headers["location"].endswith("/dashboard/settings?drive=connected")
    assert replay.headers["location"].endswith("/dashboard/settings?drive=invalid_state")


def test_callback_never_redirects_to_provider_query_value(session, monkeypatch) -> None:
    client, _ = _authenticated_client(session)
    _configure_drive(monkeypatch)
    try:
        response = client.get(
            "/api/v1/users/me/google-drive/callback",
            params={"state": "unknown", "code": "code", "error": "https://evil.example"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.headers["location"].endswith("/dashboard/settings?drive=invalid_state")
    assert "evil.example" not in response.headers["location"]


def test_callback_failure_redirects_to_dashboard_settings(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    _configure_drive(monkeypatch)
    raw_state = "failed-state-value"
    session.add(
        OAuthState(
            user_id=user.id,
            state_hash=hashlib.sha256(raw_state.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    session.commit()

    async def fail_authorization(*_: object, **__: object) -> None:
        raise GoogleDriveOAuthError("provider details")

    monkeypatch.setattr(
        GoogleDriveOAuthService, "complete_authorization", fail_authorization
    )
    try:
        response = client.get(
            "/api/v1/users/me/google-drive/callback",
            params={"state": raw_state, "code": "authorization-code"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.headers["location"].endswith(
        "/dashboard/settings?drive=authorization_failed"
    )
    assert "provider details" not in response.headers["location"]


def test_disconnect_clears_local_connection_without_deleting_backup_records(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    backup = WorkspaceBackup(
        user_id=user.id,
        remote_file_id="preserved-remote-id",
        status=BackupStatus.completed,
        archive_size_bytes=100,
        checksum="a" * 64,
    )
    session.add(backup)
    session.commit()
    _configure_drive(monkeypatch)

    async def disconnect(_: GoogleDriveOAuthService, target: GoogleDriveConnection) -> None:
        target.status = DriveConnectionStatus.disconnected
        _.session.add(target)
        _.session.commit()

    monkeypatch.setattr(GoogleDriveOAuthService, "disconnect", disconnect)
    try:
        response = client.delete("/api/v1/users/me/google-drive")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert session.get(WorkspaceBackup, backup.id).remote_file_id == "preserved-remote-id"
    session.refresh(connection)
    assert connection.status == DriveConnectionStatus.disconnected


def test_reauthorization_status_is_visible_without_provider_call(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    connection = _connection(session, user)
    connection.status = DriveConnectionStatus.reauthorization_required
    session.add(connection)
    session.commit()
    _configure_drive(monkeypatch)
    try:
        response = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["connection_status"] == "reauthorization_required"


def test_restore_hides_foreign_backup_existence(session, monkeypatch) -> None:
    client, _ = _authenticated_client(session)
    foreign = User(email="restore-foreign@example.com", hashed_password="hashed-password", is_verified=True)
    session.add(foreign)
    session.commit()
    assert foreign.id is not None
    source = WorkspaceBackup(
        user_id=foreign.id,
        status=BackupStatus.completed,
        remote_file_id="remote-file-id",
        checksum="a" * 64,
    )
    session.add(source)
    session.commit()
    _configure_drive(monkeypatch)
    try:
        response = client.post(
            f"/api/v1/users/me/backups/{source.backup_id}/restore",
            json={"confirmation": "RESTORE"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_delete_backups_authorizes_and_deletes_completed_and_incomplete_objects(
    session, monkeypatch
) -> None:
    client, user = _authenticated_client(session)
    connection = _connection(session, user)
    _configure_drive(monkeypatch)

    class Store:
        def __init__(self) -> None:
            self.authorized: list[str] = []
            self.deleted: list[str] = []

        async def list(self, owner_id: UUID) -> list[StoredBackup]:
            return [
                _remote_backup("completed-id", owner_id=owner_id),
                _remote_backup("incomplete-id", owner_id=owner_id, completed=False),
                _remote_backup("foreign-id", owner_id=uuid4()),
                StoredBackup(
                    remote_id="malformed-id",
                    name="cognolith-malformed.zip",
                    size=100,
                    created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                    metadata=BackupObjectMetadata(
                        drive_owner_id=owner_id,
                        workspace_owner_id=uuid4(),
                        backup_id=uuid4(),
                        schema_version=1,
                        archive_checksum="not-a-checksum",
                        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
                    ),
                    completed=True,
                ),
            ]

        def authorize_backup(self, backup: StoredBackup) -> None:
            self.authorized.append(backup.remote_id)

        async def delete(self, remote_id: str) -> None:
            self.deleted.append(remote_id)

        async def aclose(self) -> None:
            return None

    store = Store()
    monkeypatch.setattr("app.api.routes.backups._store", lambda *_: store)
    try:
        response = client.post(
            "/api/v1/users/me/google-drive/delete-backups",
            json={"confirmation": "DELETE BACKUPS"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert store.authorized == ["completed-id", "incomplete-id"]
    assert store.deleted == ["completed-id", "incomplete-id"]
    assert connection.status == DriveConnectionStatus.connected


def test_backup_list_orders_restore_points_and_marks_unsupported_schema_ineligible(
    session, monkeypatch
) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    _configure_drive(monkeypatch)
    supported = WorkspaceBackup(
        user_id=user.id,
        remote_file_id="supported-id",
        status=BackupStatus.completed,
        archive_size_bytes=100,
        checksum="a" * 64,
    )
    unsupported = WorkspaceBackup(
        user_id=user.id,
        remote_file_id="unsupported-id",
        status=BackupStatus.completed,
        schema_version=99,
        archive_size_bytes=100,
        checksum="a" * 64,
    )
    session.add_all([supported, unsupported])
    session.commit()
    newer = _remote_backup(
        "unsupported-id",
        owner_id=derive_drive_owner_id(connection.google_subject),
        backup_id=unsupported.backup_id,
        schema_version=99,
        created_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )
    older = _remote_backup(
        "supported-id",
        owner_id=newer.metadata.drive_owner_id,
        backup_id=supported.backup_id,
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
    )

    async def remote_backups(*_: object) -> list[StoredBackup]:
        return [older, newer]

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", remote_backups)
    try:
        response = client.get("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item["backup_id"] for item in response.json()] == [
        str(unsupported.backup_id),
        str(supported.backup_id),
    ]
    assert [item["restore_eligible"] for item in response.json()] == [False, True]
    assert "checksum" not in response.text
    assert "remote_file_id" not in response.text


def test_backup_list_adopts_trusted_remote_snapshot_for_current_user(
    session, monkeypatch
) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    _configure_drive(monkeypatch)
    owner_id = derive_drive_owner_id(connection.google_subject)
    backup_id = uuid4()
    created_at = datetime(2026, 7, 23, 8, 30, tzinfo=UTC)
    remote = _remote_backup(
        "fresh-install-remote-id",
        owner_id=owner_id,
        backup_id=backup_id,
        created_at=created_at,
    )

    async def remote_backups(*_: object) -> list[StoredBackup]:
        return [remote]

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", remote_backups)
    try:
        response = client.get("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    adopted = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user.id,
            WorkspaceBackup.backup_id == backup_id,
        )
    ).one()
    assert response.status_code == 200
    assert response.json()[0]["backup_id"] == str(backup_id)
    assert response.json()[0]["restore_eligible"] is True
    assert adopted.remote_file_id == remote.remote_id
    assert adopted.schema_version == remote.metadata.schema_version
    assert adopted.checksum == remote.metadata.archive_checksum
    assert adopted.archive_size_bytes == remote.size
    assert adopted.created_at == created_at.replace(tzinfo=None)
    assert adopted.completed_at == created_at


def test_status_adopts_remote_snapshot_once_across_repeated_requests(
    session, monkeypatch
) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    _configure_drive(monkeypatch)
    remote = _remote_backup(
        "idempotent-remote-id",
        owner_id=derive_drive_owner_id(connection.google_subject),
        backup_id=uuid4(),
    )

    async def remote_backups(*_: object) -> list[StoredBackup]:
        return [remote]

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", remote_backups)
    try:
        first = client.get("/api/v1/users/me/google-drive/status")
        second = client.get("/api/v1/users/me/google-drive/status")
    finally:
        app.dependency_overrides.clear()

    adopted = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user.id,
            WorkspaceBackup.backup_id == remote.metadata.backup_id,
        )
    ).all()
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["restore_points"][0]["restore_eligible"] is True
    assert second.json()["restore_points"][0]["restore_eligible"] is True
    assert len(adopted) == 1


def test_remote_adoption_is_race_safe_across_database_sessions(
    session, monkeypatch
) -> None:
    _, user = _authenticated_client(session)
    assert user.id is not None
    user_id = user.id
    owner_id = uuid4()
    remote = _remote_backup(
        "concurrent-remote-id",
        owner_id=owner_id,
        backup_id=uuid4(),
    )

    def adopt() -> None:
        with Session(engine) as concurrent_session:
            _adopt_remote_backups(
                concurrent_session, user_id, owner_id, [remote]
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: adopt(), range(2)))
    finally:
        app.dependency_overrides.clear()

    session.expire_all()
    adopted = session.exec(
        select(WorkspaceBackup).where(
            WorkspaceBackup.user_id == user_id,
            WorkspaceBackup.backup_id == remote.metadata.backup_id,
        )
    ).all()
    assert len(adopted) == 1


def test_backup_list_never_adopts_untrusted_remote_objects(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    _configure_drive(monkeypatch)
    owner_id = derive_drive_owner_id(connection.google_subject)
    incomplete = _remote_backup("incomplete-remote-id", owner_id=owner_id, completed=False)
    foreign = _remote_backup("foreign-remote-id", owner_id=uuid4())
    malformed = StoredBackup(
        remote_id="malformed-remote-id",
        name="cognolith-malformed.zip",
        size=100,
        created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        metadata=BackupObjectMetadata(
            drive_owner_id=owner_id,
            workspace_owner_id=uuid4(),
            backup_id=uuid4(),
            schema_version=1,
            archive_checksum="not-a-checksum",
            created_at=datetime(2026, 7, 24, 12, 0, tzinfo=UTC),
        ),
        completed=True,
    )

    async def remote_backups(*_: object) -> list[StoredBackup]:
        return [incomplete, foreign, malformed]

    monkeypatch.setattr("app.api.routes.backups._trusted_remote_backups", remote_backups)
    try:
        response = client.get("/api/v1/users/me/backups")
    finally:
        app.dependency_overrides.clear()

    adopted = session.exec(
        select(WorkspaceBackup).where(WorkspaceBackup.user_id == user.id)
    ).all()
    assert response.status_code == 200
    assert response.json() == []
    assert adopted == []


def test_operation_status_exposes_each_durable_phase(session) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    operations = [
        WorkspaceBackup(user_id=user.id, status=status)
        for status in (
            BackupStatus.pending,
            BackupStatus.exporting,
            BackupStatus.uploading,
            BackupStatus.completed,
            BackupStatus.failed,
        )
    ]
    session.add_all(operations)
    session.commit()

    try:
        responses = [
            client.get(f"/api/v1/users/me/backups/operations/{operation.backup_id}")
            for operation in operations
        ]
    finally:
        app.dependency_overrides.clear()

    assert [response.status_code for response in responses] == [200] * len(operations)
    assert [response.json()["status"] for response in responses] == [
        "pending",
        "exporting",
        "uploading",
        "completed",
        "failed",
    ]


def test_preview_maps_disconnected_connection_to_safe_reauthorization_error(session, monkeypatch) -> None:
    client, user = _authenticated_client(session)
    assert user.id is not None
    connection = _connection(session, user)
    connection.status = DriveConnectionStatus.disconnected
    source = WorkspaceBackup(
        user_id=user.id,
        remote_file_id="source-id",
        status=BackupStatus.completed,
        archive_size_bytes=100,
        checksum="a" * 64,
    )
    session.add(source)
    session.add(connection)
    session.commit()
    _configure_drive(monkeypatch)

    try:
        response = client.get(f"/api/v1/users/me/backups/{source.backup_id}/preview")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["error"] == "GOOGLE_DRIVE_REAUTHORIZATION_REQUIRED"
