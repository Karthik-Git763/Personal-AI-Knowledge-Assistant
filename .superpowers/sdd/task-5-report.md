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
