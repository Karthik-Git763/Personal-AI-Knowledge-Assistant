from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

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

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_CHUNK_SIZE = 64 * 1024


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode one JSON record in the stable format used by backups."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
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


def _source_candidate(upload_root: Path, source: Path) -> tuple[Path, Path]:
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
    return root, candidate


@contextmanager
def open_validated_source(upload_root: Path, source: Path) -> Generator[IO[bytes], None, None]:
    """Open and validate one regular upload, then yield that same descriptor."""
    root, candidate = _source_candidate(upload_root, source)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise UnsafeBackupPath("Document source could not be safely opened") from error

    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(candidate)
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(descriptor_stat.st_mode):
            raise UnsafeBackupPath("Document source is not a regular file")
        if not os.path.samestat(path_stat, descriptor_stat):
            raise UnsafeBackupPath("Document source changed while it was being opened")
        resolved_root = root.resolve()
        resolved_source = candidate.resolve(strict=True)
        if not _is_relative_to(resolved_source, resolved_root):
            raise UnsafeBackupPath("Document source is outside the upload directory")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file:
            yield input_file
    finally:
        os.close(descriptor)


def validate_archive_path(upload_root: Path, source: Path) -> Path:
    """Validate a source path for callers that only need a containment check."""
    root, candidate = _source_candidate(upload_root, source)
    with open_validated_source(root, candidate):
        return candidate.resolve(strict=True)


class _BoundedZipFile:
    def __init__(self, output: IO[bytes], maximum_size: int) -> None:
        self.output = output
        self.maximum_size = maximum_size

    def write(self, data: bytes) -> int:
        if self.output.tell() + len(data) > self.maximum_size:
            raise ArchiveSizeExceeded("Archive exceeds configured size limit")
        return self.output.write(data)

    def flush(self) -> None:
        self.output.flush()

    def close(self) -> None:
        self.output.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.output, name)


class VersionedZipWriter:
    """Write stable, checked archive entries while keeping record payloads streamed."""

    def __init__(self, destination: Path, maximum_size: int) -> None:
        self.destination = destination
        self.maximum_size = maximum_size
        self._archive: ZipFile | None = None
        self._output: IO[bytes] | None = None
        self._names: set[str] = set()

    def __enter__(self) -> VersionedZipWriter:
        if self.maximum_size < 1:
            raise ArchiveSizeExceeded("Archive size limit must be positive")
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self._output = self.destination.open("w+b")
        self._archive = ZipFile(
            _BoundedZipFile(self._output, self.maximum_size),
            mode="w",
            compression=ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        )
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            if self._archive is not None:
                self._archive.close()
            if self._output is not None:
                self._output.close()
            if exc_type is None and self.destination.stat().st_size > self.maximum_size:
                raise ArchiveSizeExceeded("Archive exceeds configured size limit")
        except Exception:
            self.destination.unlink(missing_ok=True)
            raise
        if exc_type is not None:
            self.destination.unlink(missing_ok=True)
        return False

    def write_bytes(self, name: str, data: bytes) -> str:
        digest = hashlib.sha256()
        with self._open_entry(name) as output:
            output.write(data)
            digest.update(data)
        return digest.hexdigest()

    def write_json_array(self, name: str, records: Iterable[Mapping[str, Any]]) -> tuple[str, int]:
        digest = hashlib.sha256()
        count = 0
        with self._open_entry(name) as output:
            output.write(b"[")
            digest.update(b"[")
            for record in records:
                if count:
                    output.write(b",")
                    digest.update(b",")
                payload = canonical_json_bytes(record)
                output.write(payload)
                digest.update(payload)
                count += 1
            output.write(b"]")
            digest.update(b"]")
        return digest.hexdigest(), count

    def write_fileobj(self, name: str, source: IO[bytes]) -> str:
        digest = hashlib.sha256()
        with self._open_entry(name) as output:
            while chunk := source.read(_CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
        return digest.hexdigest()

    def _open_entry(self, name: str) -> IO[bytes]:
        if name in self._names:
            raise DuplicateArchiveName(f"Duplicate archive entry: {name}")
        if self._archive is None:
            raise RuntimeError("Archive writer is not open")
        self._names.add(name)
        entry = ZipInfo(filename=name, date_time=_ZIP_TIMESTAMP)
        entry.compress_type = ZIP_DEFLATED
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        return self._archive.open(entry, mode="w")
