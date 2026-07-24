# Task 4 Report: Drive Storage Adapter And Retention

Status: complete

Files:
- `backend/app/services/backup_store.py`
- `backend/app/services/google_drive_store.py`
- `backend/tests/fakes/__init__.py`
- `backend/tests/fakes/fake_backup_store.py`
- `backend/tests/test_google_drive_store.py`
- `backend/tests/test_backup_retention.py`

RED/GREEN evidence:
- RED: `docker compose exec backend pytest -q tests/test_google_drive_store.py tests/test_backup_retention.py` initially failed during collection because `app.services.backup_store` did not exist.
- RED: the download transport-error regression failed with an unwrapped `httpx.ConnectError` before the streaming boundary mapped request failures to `GoogleDriveRetryableError`.
- GREEN: `docker compose exec backend pytest -q tests/test_google_drive_store.py tests/test_backup_retention.py` completed with `15 passed in 12.56s`.
- Static checks: focused Ruff completed with `All checks passed!`; BasedPyright completed with `0 errors, 0 warnings, 0 notes`.

Self-review:
- Confirmed the Drive adapter uses only `httpx`, a resumable upload under `appDataFolder`, streamed upload/download bodies, validated app properties, escaped owner filtering, and no provider response-body logging.
- Confirmed remote deletion requires a valid trusted ID from a filtered list, successful upload, or an explicit locally validated record registration.
- Confirmed downloads enforce the configured size limit before writing excess data, fsync the temporary sibling, atomically replace the destination, and remove temporary files on all failure paths.
- Confirmed retention considers only valid, completed backups for the requested owner, keeps the five newest by metadata timestamp, preserves incomplete/foreign/malformed entries, and raises a cleanup-specific error without changing backup completion state.
- Confirmed `git diff --check` completed without diff errors and the committed scope is limited to Task 4 files and this report.

Concerns:
- The adapter is intentionally not connected to backup-record persistence or scheduling; that coordination is outside Task 4.

## Critical/Important Finding Remediation

Status: complete

Changes:
- Logical backup time now comes solely from the validated `appProperties.created_at`; Drive `createdTime` is parsed independently and fixtures model the normal case where it differs.
- Drive payload validation now requires a parent list containing `appDataFolder`, and completion PATCH responses must retain the resumable upload's remote ID.
- Download creates and closes its temporary handle before authorization or streaming, writes through a separate scoped handle, and suppresses cleanup-only failures while preserving the primary exception.
- String-only trust registration was replaced with `authorize_backup`, which accepts only a valid owner-matching `StoredBackup` or a completed, owner-matching, locally complete `WorkspaceBackup`. Filtered lists and successful uploads remain trusted sources.
- OAuth refresh now encrypts and persists rotated refresh tokens, distinguishes retryable token-endpoint failures from revocation, and the store preserves that distinction as retryable versus reauthorization-required.

RED/GREEN evidence:
- RED: the new OAuth regressions initially failed collection because the retryable and reauthorization-specific OAuth errors did not exist.
- Regression coverage: the added assertions encode the prior timestamp coupling, missing parent check, PATCH-ID acceptance, string-only authorization, non-atomic success path, and refresh classification gaps.
- GREEN: `docker compose exec backend pytest -q tests/test_google_drive_store.py tests/test_backup_retention.py tests/test_google_drive_oauth.py` completed successfully with all focused Task 4 and OAuth tests passing.
- Static checks: focused Ruff completed with `All checks passed!`; BasedPyright completed with `0 errors, 0 warnings, 0 notes`.

Self-review:
- Verified `createdTime` only validates provider payload shape while `StoredBackup.created_at` retains the logical application timestamp.
- Verified temporary-file cleanup cannot replace an active authorization, stream, size-limit, or provider exception.
- Verified all newly trusted delete IDs originate from a filtered list, completed upload, or validated backup identity.

Concerns:
- No remaining implementation concerns identified; task-level backup record coordination and scheduling remain intentionally out of scope.
