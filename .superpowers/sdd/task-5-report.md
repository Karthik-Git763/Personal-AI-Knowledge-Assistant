# Task 5 Report: Backup Coordinator And Scheduler

Status: complete

Files:
- `backend/app/services/backup_coordinator.py`
- `backend/app/services/backup_scheduler.py`
- `backend/app/main.py`
- `backend/tests/test_backup_coordinator.py`
- `backend/tests/test_backup_scheduler.py`
- `.superpowers/sdd/task-5-report.md`

RED/GREEN evidence:
- RED: `docker compose exec backend pytest -q tests/test_backup_coordinator.py tests/test_backup_scheduler.py` initially failed during collection because `app.services.backup_coordinator` and `app.services.backup_scheduler` did not exist.
- GREEN: `docker compose exec backend pytest -q tests/test_backup_coordinator.py tests/test_backup_scheduler.py -rA` completed with `14 passed in 17.30s`.
- Static checks: focused Ruff completed with `All checks passed!`; BasedPyright completed with `0 errors, 0 warnings, 0 notes`.

Self-review:
- Confirmed a durable pending operation is created before work, duplicate user triggers collapse, and a PostgreSQL per-user advisory lock spans export, upload, committed completion, and retention before release.
- Confirmed verified metadata persistence, restrictive temporary paths, generic sanitized failures, temp cleanup, retention behavior, schedule timestamps, and revoked credential handling are covered by focused tests.
- Confirmed due schedules use `FOR UPDATE SKIP LOCKED`, are claimed before dispatch, process users independently, and the lifespan-owned loop stops through cancellation without logging it as a failure.
- Confirmed `git diff --check` completed without diff errors and only Task 5 implementation/test/report files are staged for commit.

Concerns:
- The completed schema only persists `pending`, `running`, `completed`, and `failed`, so the coordinator represents export and upload as the durable `running` phase rather than separate persisted statuses.
- The completed connection schema has no `reauthorization_required` enum value; revoked credentials are persisted as `failed` with a sanitized reauthorization failure message. Adding the literal requested status requires an out-of-scope schema migration.

## Critical/Important Remediation

Status: complete

Files:
- `backend/alembic/versions/0005_google_drive_backup_foundation.py`
- `backend/app/models/backup.py`
- `backend/app/schemas/backup.py`
- `backend/app/services/backup_coordinator.py`
- `backend/tests/test_backup_coordinator.py`
- `backend/tests/test_backup_scheduler.py`
- `backend/tests/test_backup_models.py`
- `backend/tests/test_migrations.py`
- `.superpowers/sdd/task-5-report.md`

RED/GREEN evidence:
- RED: `docker compose exec backend pytest -q tests/test_backup_coordinator.py tests/test_backup_scheduler.py tests/test_backup_models.py tests/test_migrations.py` failed because `WorkspaceBackup.backup_id` was not yet present; after moving the UUID field to the operation model, the focused first coordinator test passed.
- RED: `docker compose exec backend pytest -q tests/test_backup_coordinator.py::test_retention_cancellation_keeps_completed_backup_and_schedule_success` failed with the completed backup overwritten to `failed` during retention cancellation.
- GREEN: `docker compose exec backend pytest -q tests/test_backup_coordinator.py tests/test_backup_scheduler.py tests/test_backup_models.py tests/test_migrations.py` completed with `29 passed in 20.74s`.
- Static checks: focused Ruff completed with `All checks passed!`; BasedPyright completed with `0 errors, 0 warnings, 0 notes`.

Self-review:
- Confirmed `WorkspaceBackup.backup_id` is a unique UUID public operation identifier while the integer primary key remains internal, and coordinator run lookup uses the UUID.
- Confirmed `exporting` and `uploading` are committed before their respective work; manifest ownership is checked before upload; revoked credentials persist as `reauthorization_required` in the widened unreleased migration.
- Confirmed ordinary retention list/delete failures record only a sanitized warning after committed completion; post-completion cancellation does the same while re-raising; in-flight cancellation becomes a sanitized failed interruption and cleans temporary files/releases the advisory lock.
- Confirmed a separate PostgreSQL session holding `FOR UPDATE` causes scheduler `SKIP LOCKED` to dispatch no duplicate work.

Concerns:
- No remaining known Critical/Important concerns in the Task 5 remediation scope.
