# Task 6 Report

## Status

Complete. Safe backup preview and replacement restore are implemented on branch
`backup`.

Base SHA: `b26bdf2`

Implementation SHA: `48d64d8928754cd4a9e20a037e5b6678264f42c0`

## Scope

- Added strict, metadata-only archive preview validation.
- Added transactional replacement restore with explicit filesystem and database
  rollback behavior, crash recovery journaling, and derived-data reset.
- Added restore orchestration, confirmation, ownership checks, safety backup,
  lifecycle guards, and sanitized operation status.
- Added restore operation model, migration, and API schema fields.
- Added focused importer, restore, model, and migration coverage.

## TDD Evidence

RED evidence:

- Importer preview tests initially failed because
  `app.services.backup_importer` did not exist.
- Restore contract tests initially failed because `BackupRestoreFailed` did not
  exist.
- Coordinator restore tests initially failed because `BackupOperationKind` did
  not exist.
- Ten adversarial archive/filesystem tests failed before compression,
  non-finite JSON, staging TOCTOU, cross-device move, and equivalent-path
  defenses were added.
- Two lifecycle tests failed before pending restore operations blocked snapshots
  and `run_backup` rejected restore operations.
- Three schema/limit tests failed before strict confirmation and string
  validation were added.

GREEN evidence:

- `docker compose exec backend pytest -q tests/test_backup_importer.py tests/test_backup_restore.py`
  - `52 passed in 20.74s`
- `docker compose exec backend pytest -q tests/test_backup_coordinator.py tests/test_backup_models.py tests/test_migrations.py`
  - `26 passed in 29.27s`
- `docker compose exec backend ruff check app/services/backup_importer.py app/services/backup_coordinator.py app/models/backup.py app/schemas/backup.py tests/test_backup_importer.py tests/test_backup_restore.py`
  - `All checks passed!`
- `docker compose exec backend ruff check tests/test_backup_models.py tests/test_migrations.py alembic/versions/0005_google_drive_backup_foundation.py`
  - `All checks passed!`
- `docker compose exec backend basedpyright app/services/backup_importer.py app/services/backup_coordinator.py app/models/backup.py app/schemas/backup.py tests/test_backup_importer.py tests/test_backup_restore.py`
  - `0 errors, 0 warnings, 0 notes`
- `git diff --cached --check`
  - Passed.

## Self-review

Reviewed archive trust boundaries, path confinement, staged-file integrity,
cross-device moves, transaction ordering, rollback cleanup, post-commit cleanup,
owner isolation, lifecycle guards, and operation status sanitization against the
Task 6 brief. No unresolved findings remain.

## Concerns

None identified.

## Critical and Important Review Fixes

Status: Complete.

Review-fix base SHA: `48d64d8928754cd4a9e20a037e5b6678264f42c0`

### Changes

- Added a dedicated PostgreSQL transaction advisory lock around every importer
  restore and its recovery scan. The existing coordinator per-user session lock
  remains in place. Concurrent restores for different users cannot enter
  recovery while another restore has a live journal.
- Made restore operation completion part of the importer's workspace
  replacement transaction. The importer now requires the persisted restore
  operation, writes completed status, timestamp, schema, archive size, and item
  counts before the replacement commit, and the coordinator performs no second
  success commit.
- Prevented post-commit journal cleanup errors from entering rollback cleanup
  after the workspace and operation have committed.
- Hardened final document destinations by rejecting symlinked parent
  directories, resolving and confining parents under the upload root, opening
  final names exclusive and no-follow where supported, and streaming EXDEV
  fallback copies into the already validated descriptor.
- Validated `ZipInfo.orig_filename` before the normalized filename so NUL-based
  raw ZIP names are rejected.
- Documented and tested that restore preview may persist valid Google OAuth
  refresh bookkeeping while leaving workspace durable rows and files
  unchanged.

### TDD Evidence

RED:

- The two-session concurrent-user regression showed the second restore entering
  recovery while the first restore's journal was live.
- The stale-pending regression failed because the importer did not accept or
  complete a restore operation.
- Coordinator atomicity regressions failed because the operation was not passed
  into the importer and success required a second commit.
- The symlink-parent regression restored through a user-directory symlink into
  an external directory.
- The crafted `notes.json\0evil` raw ZIP entry passed validation because only
  the truncated `filename` was checked.
- An injected post-commit journal unlink error removed newly committed files and
  reported restore failure.

GREEN:

- `docker compose exec backend pytest -q tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `74 passed in 34.76s`
- `docker compose exec backend ruff check app/services/backup_importer.py app/services/backup_coordinator.py tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `All checks passed!`
- `docker compose exec backend basedpyright app/services/backup_importer.py app/services/backup_coordinator.py tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `0 errors, 0 warnings, 0 notes`
- `git diff --check`
  - Passed; only repository line-ending notices were emitted.

### Review

Rechecked lock acquisition and release, crash-journal visibility, transaction
commit ordering, coordinator failure marking, descriptor lifetime, EXDEV copy
behavior, symlink confinement, raw ZIP-name validation, and preview mutation
boundaries. No unresolved findings remain.

## Final Important Review Fixes

Status: Complete.

Review-fix base SHA: `7cf821b0e617fb8f8f070bb2afff1b195e03a0d1`

Implementation commit: the commit containing this report section.

### Changes

- Made recovery-journal creation power-loss durable by fsyncing the journal file
  and its parent directory before any final file placement.
- Made every final document placement durable before the database commit by
  reopening and fsyncing the placed file and then fsyncing its validated parent
  directory. This applies after same-device rename, exclusive EXDEV copy, and
  injected move implementations.
- Limited ignored directory-fsync failures to documented unsupported errnos:
  `EINVAL`, `ENOTSUP`, and `EOPNOTSUPP`, plus Windows-only `EACCES` and `EPERM`.
  Other I/O failures continue to abort and roll back the restore.
- Made post-commit old-file cleanup explicitly best-effort across surviving-path
  database lookup, upload-root resolution, and unlink failures. Failures are
  logged with a fixed message and exception type only, failed cleanup
  transactions are reset, and the committed restore operation remains
  completed.

### TDD Evidence

RED:

- The durability ordering regression observed only two file fsyncs and no
  directory fsyncs before commit; the injected second-directory `EIO` never
  fired. Result: `2 failed, 26 deselected in 2.99s`.
- The post-commit cleanup regressions showed query and root-resolution failures
  escaping after commit and unlink failure remaining unrecorded. Result:
  `3 failed, 28 deselected in 4.56s`.

GREEN:

- Durability ordering and pre-commit failure regressions:
  `2 passed, 26 deselected in 2.84s`.
- Query, root-resolution, and unlink cleanup regressions:
  `3 passed, 28 deselected in 7.56s`.
- Unsupported-directory-fsync regression:
  `1 passed, 31 deselected in 1.73s`.
- `docker compose exec backend pytest -q tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `80 passed in 40.59s`
- `docker compose exec backend ruff check app/services/backup_importer.py app/services/backup_coordinator.py tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `All checks passed!`
- `docker compose exec backend basedpyright app/services/backup_importer.py app/services/backup_coordinator.py tests/test_backup_importer.py tests/test_backup_restore.py tests/test_backup_coordinator.py`
  - `0 errors, 0 warnings, 0 notes`

### Self-review

Rechecked journal durability ordering, descriptor lifetimes, same-device rename
and EXDEV copy synchronization, unsupported directory-fsync errno handling,
pre-commit rollback cleanup, post-commit transaction reset, sanitized logging,
and completed-operation preservation. No unresolved findings remain.

### Concerns

None identified.
