from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from pydantic import BaseModel


class BackupArchiveError(Exception):
    """Base error for a backup archive that cannot be safely created."""


class UnsafeBackupPathError(BackupArchiveError):
    """A document source cannot be read safely from the upload directory."""


class DuplicateArchiveNameError(BackupArchiveError):
    """An archive entry would overwrite an existing entry."""


class ArchiveSizeExceededError(BackupArchiveError):
    """The completed archive exceeds its configured size limit."""


class BackupManifestV1(BaseModel):
    schema_version: Literal[1]
    backup_id: UUID
    owner_id: UUID
    created_at: datetime
    app_version: str
    counts: dict[str, int]
    checksums: dict[str, str]
    record_filenames: dict[str, str]


class BackupExportResult(BaseModel):
    path: Path
    manifest: BackupManifestV1
    archive_checksum: str
    archive_size: int


UnsafeBackupPath = UnsafeBackupPathError
DuplicateArchiveName = DuplicateArchiveNameError
ArchiveSizeExceeded = ArchiveSizeExceededError


def canonical_json_bytes(value: Mapping[str, Any] | list[dict[str, Any]]) -> bytes:
    """Encode a record collection in the stable JSON format used by backups."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_archive_filename(filename: str) -> str:
    """Return a normalized single filename without accepting uploaded path parts."""
    normalized = unicodedata.normalize("NFKC", filename).replace("\\", "/")
    name = PurePosixPath(normalized).name
    if name in {"", ".", ".."}:
        return "document"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "document"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_archive_path(upload_root: Path, source: Path) -> Path:
    """Resolve an existing regular upload without traversing paths or symlinks."""
    root = upload_root.absolute()
    candidate = source if source.is_absolute() else source.absolute()
    if not _is_relative_to(candidate, root):
        candidate = (root / source).absolute()

    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise UnsafeBackupPath("Document source is outside the upload directory") from error
    if ".." in relative.parts:
        raise UnsafeBackupPath("Document source is outside the upload directory")

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise UnsafeBackupPath("Document source cannot be a symlink")

    if not candidate.exists():
        raise FileNotFoundError(f"Document source is missing: {candidate}")
    if not candidate.is_file():
        raise UnsafeBackupPath("Document source is not a regular file")

    resolved_root = root.resolve()
    resolved_source = candidate.resolve(strict=True)
    if not _is_relative_to(resolved_source, resolved_root):
        raise UnsafeBackupPath("Document source is outside the upload directory")
    return resolved_source


class VersionedZipWriter:
    """Write checked archive entries without keeping source files in memory."""

    def __init__(self, destination: Path, maximum_size: int) -> None:
        self.destination = destination
        self.maximum_size = maximum_size
        self._archive: ZipFile | None = None
        self._names: set[str] = set()

    def __enter__(self) -> VersionedZipWriter:
        if self.maximum_size < 1:
            raise ArchiveSizeExceeded("Archive size limit must be positive")
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self._archive = ZipFile(self.destination, mode="w", compression=ZIP_DEFLATED, allowZip64=True)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            if self._archive is not None:
                self._archive.close()
            if exc_type is None:
                self._ensure_size()
        except Exception:
            self.destination.unlink(missing_ok=True)
            raise
        if exc_type is not None:
            self.destination.unlink(missing_ok=True)
        return False

    def write_bytes(self, name: str, data: bytes) -> str:
        archive = self._prepare_entry(name)
        archive.writestr(name, data)
        self._ensure_size()
        return hashlib.sha256(data).hexdigest()

    def write_file(self, name: str, source: Path) -> str:
        archive = self._prepare_entry(name)
        digest = hashlib.sha256()
        with source.open("rb") as input_file, archive.open(name, mode="w") as output_file:
            while chunk := input_file.read(64 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
        self._ensure_size()
        return digest.hexdigest()

    def _prepare_entry(self, name: str) -> ZipFile:
        if name in self._names:
            raise DuplicateArchiveName(f"Duplicate archive entry: {name}")
        if self._archive is None:
            raise RuntimeError("Archive writer is not open")
        self._names.add(name)
        return self._archive

    def _ensure_size(self) -> None:
        if self.destination.exists() and self.destination.stat().st_size > self.maximum_size:
            raise ArchiveSizeExceeded("Archive exceeds configured size limit")
