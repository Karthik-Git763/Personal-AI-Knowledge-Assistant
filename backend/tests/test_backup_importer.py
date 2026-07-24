from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from app.services.backup_exporter import RECORD_FILENAMES
from app.services.backup_importer import BackupImporter, UnsafeBackupArchive


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _records() -> dict[str, list[dict[str, Any]]]:
    return {name: [] for name in RECORD_FILENAMES}


def _write_archive(
    path: Path,
    *,
    owner_id: UUID,
    records: dict[str, list[dict[str, Any]]] | None = None,
    app_version: str = "0.1.0",
    schema_version: int = 1,
    extra_entries: list[tuple[str, bytes, int | None]] | None = None,
    manifest_updates: dict[str, Any] | None = None,
) -> Path:
    archive_records = records or _records()
    payloads = {
        RECORD_FILENAMES[name]: _json_bytes(value) for name, value in archive_records.items()
    }
    for name, payload, _ in extra_entries or []:
        payloads[name] = payload
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "backup_id": str(uuid4()),
        "owner_id": str(owner_id),
        "created_at": datetime(2026, 7, 24, 12, 0, tzinfo=UTC).isoformat(),
        "app_version": app_version,
        "counts": {name: len(value) for name, value in archive_records.items()},
        "checksums": {
            name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()
        },
        "record_filenames": RECORD_FILENAMES,
    }
    manifest.update(manifest_updates or {})

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            mode = next(
                (
                    entry_mode
                    for entry_name, _, entry_mode in extra_entries or []
                    if entry_name == name
                ),
                None,
            )
            if mode is None:
                archive.writestr(name, payload)
            else:
                info = ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, payload)
        archive.writestr("manifest.json", _json_bytes(manifest))
    return path


@pytest.fixture
def owner_id() -> UUID:
    return uuid4()


@pytest.fixture
def importer(tmp_path: Path) -> BackupImporter:
    return BackupImporter(
        maximum_archive_size=1024 * 1024,
        maximum_expanded_size=1024 * 1024,
        maximum_entry_size=512 * 1024,
        maximum_entry_count=32,
        maximum_compression_ratio=50,
        maximum_json_depth=8,
        maximum_json_string_length=1024,
        maximum_json_collection_size=64,
        supported_app_versions={"0.1.0"},
        upload_root=tmp_path / "uploads",
        temporary_directory=tmp_path / "backup-temp",
    )


def test_preview_returns_only_safe_metadata(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive = _write_archive(tmp_path / "valid.zip", owner_id=owner_id)

    preview = importer.preview(archive, expected_workspace_owner_id=owner_id)

    assert preview.schema_version == 1
    assert preview.app_version == "0.1.0"
    assert preview.archive_size_bytes == archive.stat().st_size
    assert preview.item_counts == {name: 0 for name in RECORD_FILENAMES}
    assert preview.created_at == datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    assert preview.warnings == ["Document-derived content will be rebuilt after restore."]
    assert not (tmp_path / "uploads").exists()
    assert not (tmp_path / "backup-temp").exists()


def test_preview_rejects_archive_identity_mismatch(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive_backup_id = uuid4()
    archive = _write_archive(
        tmp_path / "identity-mismatch.zip",
        owner_id=owner_id,
        manifest_updates={"backup_id": str(archive_backup_id)},
    )

    with pytest.raises(UnsafeBackupArchive, match="identity"):
        importer.preview(
            archive,
            expected_workspace_owner_id=owner_id,
            expected_archive_backup_id=uuid4(),
        )

    preview = importer.preview(
        archive,
        expected_workspace_owner_id=owner_id,
        expected_archive_backup_id=archive_backup_id,
    )
    assert preview.schema_version == 1


@pytest.mark.parametrize(
    "entry_name",
    [
        "../escape.json",
        "/absolute.json",
        "C:/drive.json",
        "files\\..\\escape.json",
        "files/\x00escape.json",
    ],
)
def test_preview_rejects_unsafe_paths(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID, entry_name: str
) -> None:
    archive = _write_archive(
        tmp_path / "unsafe.zip",
        owner_id=owner_id,
        extra_entries=[(entry_name, b"payload", None)],
    )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_nul_in_raw_zip_entry_name(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    valid = _write_archive(tmp_path / "valid.zip", owner_id=owner_id)
    crafted = tmp_path / "raw-nul.zip"
    with ZipFile(valid) as source, ZipFile(
        crafted, "w", ZIP_DEFLATED
    ) as output:
        for info in source.infolist():
            name = (
                "notes.jsonAAAAA"
                if info.filename == "notes.json"
                else info.filename
            )
            output.writestr(name, source.read(info))
    raw = crafted.read_bytes()
    assert raw.count(b"notes.jsonAAAAA") == 2
    crafted.write_bytes(
        raw.replace(b"notes.jsonAAAAA", b"notes.json\x00evil")
    )
    with ZipFile(crafted) as source:
        notes = next(
            info
            for info in source.infolist()
            if info.filename == "notes.json"
        )
        assert notes.orig_filename == "notes.json\x00evil"

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(crafted, expected_workspace_owner_id=owner_id)


def test_preview_rejects_duplicate_normalized_names(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive = _write_archive(tmp_path / "duplicate.zip", owner_id=owner_id)
    with ZipFile(archive, "a") as output:
        output.writestr("NOTES.JSON", b"[]")

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


@pytest.mark.parametrize("mode", [0o120777, 0o020666, 0o100755])
def test_preview_rejects_special_or_executable_entries(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID, mode: int
) -> None:
    archive = _write_archive(
        tmp_path / "special.zip",
        owner_id=owner_id,
        extra_entries=[("files/documents/unknown/payload", b"x", mode)],
    )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_non_zip_signature(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive = tmp_path / "not-a-zip.zip"
    archive.write_bytes(b"not a zip")

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("schema_version", 2),
        ("app_version", "99.0.0"),
        ("owner_id", str(uuid4())),
    ],
)
def test_preview_rejects_unsupported_or_mismatched_manifest(
    importer: BackupImporter,
    tmp_path: Path,
    owner_id: UUID,
    keyword: str,
    value: Any,
) -> None:
    if keyword == "owner_id":
        archive = _write_archive(
            tmp_path / "manifest.zip",
            owner_id=owner_id,
            manifest_updates={"owner_id": value},
        )
    elif keyword == "schema_version":
        archive = _write_archive(
            tmp_path / "manifest.zip",
            owner_id=owner_id,
            schema_version=value,
        )
    else:
        archive = _write_archive(
            tmp_path / "manifest.zip",
            owner_id=owner_id,
            app_version=value,
        )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


@pytest.mark.parametrize(
    "manifest_updates",
    [
        {"counts": {"notes": 1}},
        {"checksums": {}},
        {"unexpected": "field"},
        {"schema_version": "1"},
    ],
)
def test_preview_rejects_manifest_shape_count_and_checksum_mismatches(
    importer: BackupImporter,
    tmp_path: Path,
    owner_id: UUID,
    manifest_updates: dict[str, Any],
) -> None:
    archive = _write_archive(
        tmp_path / "mismatch.zip",
        owner_id=owner_id,
        manifest_updates=manifest_updates,
    )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_record_checksum_mismatch(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive = _write_archive(tmp_path / "checksum.zip", owner_id=owner_id)
    with ZipFile(archive) as source:
        payloads = {
            info.filename: source.read(info)
            for info in source.infolist()
        }
    payloads["notes.json"] = b"[{}]"
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for name, payload in payloads.items():
            output.writestr(name, payload)

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_archive_and_entry_limits(tmp_path: Path, owner_id: UUID) -> None:
    archive = _write_archive(tmp_path / "limits.zip", owner_id=owner_id)
    importer = BackupImporter(
        maximum_archive_size=archive.stat().st_size - 1,
        maximum_expanded_size=1024 * 1024,
        maximum_entry_size=512 * 1024,
        maximum_entry_count=32,
        maximum_compression_ratio=50,
        supported_app_versions={"0.1.0"},
    )
    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)

    ratio_archive = _write_archive(
        tmp_path / "ratio.zip",
        owner_id=owner_id,
        extra_entries=[("files/documents/unknown/repeated.txt", b"A" * 10_000, None)],
    )
    ratio_importer = BackupImporter(
        maximum_archive_size=1024 * 1024,
        maximum_expanded_size=1024 * 1024,
        maximum_entry_size=20_000,
        maximum_entry_count=32,
        maximum_compression_ratio=2,
        supported_app_versions={"0.1.0"},
    )
    with pytest.raises(UnsafeBackupArchive):
        ratio_importer.preview(ratio_archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_json_bounds_and_unknown_record_fields(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    records = _records()
    records["tags"] = [
        {
            "id": str(uuid4()),
            "name": "x" * 1025,
            "color": None,
            "description": None,
            "created_at": "2026-07-24T12:00:00Z",
        }
    ]
    archive = _write_archive(tmp_path / "json-bounds.zip", owner_id=owner_id, records=records)

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)

    records["tags"][0]["name"] = "tag"
    records["tags"][0]["unknown"] = True
    archive = _write_archive(tmp_path / "unknown-field.zip", owner_id=owner_id, records=records)
    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_duplicate_portable_ids_and_broken_relations(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    duplicate_id = uuid4()
    records = _records()
    records["folders"] = [
        {
            "id": str(duplicate_id),
            "created_at": "2026-07-24T12:00:00Z",
            "updated_at": "2026-07-24T12:00:00Z",
            "name": "one",
            "description": None,
            "parent_folder_id": None,
            "color": None,
            "icon": None,
            "emoji": None,
            "is_shared": False,
            "is_archived": False,
            "sort_order": 0,
            "is_deleted": False,
        },
        {
            "id": str(duplicate_id),
            "created_at": "2026-07-24T12:00:00Z",
            "updated_at": "2026-07-24T12:00:00Z",
            "name": "two",
            "description": None,
            "parent_folder_id": None,
            "color": None,
            "icon": None,
            "emoji": None,
            "is_shared": False,
            "is_archived": False,
            "sort_order": 0,
            "is_deleted": False,
        },
    ]
    archive = _write_archive(tmp_path / "duplicate-id.zip", owner_id=owner_id, records=records)
    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)

    records["folders"] = [records["folders"][0] | {"parent_folder_id": str(uuid4())}]
    archive = _write_archive(tmp_path / "broken-relation.zip", owner_id=owner_id, records=records)
    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_validates_original_document_file(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    document_id = uuid4()
    records = _records()
    records["documents"] = [
        {
            "id": str(document_id),
            "created_at": "2026-07-24T12:00:00Z",
            "updated_at": "2026-07-24T12:00:00Z",
            "title": "Document",
            "file_name": "document.txt",
            "file_size": 4,
            "file_type": "txt",
            "mime_type": "text/plain",
            "tags": [],
            "language": "en",
            "is_deleted": False,
        }
    ]
    archive = _write_archive(
        tmp_path / "document.zip",
        owner_id=owner_id,
        records=records,
        extra_entries=[(f"files/documents/{document_id}/document.txt", b"wrong", None)],
    )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_rejects_unsupported_zip_compression(
    importer: BackupImporter, tmp_path: Path, owner_id: UUID
) -> None:
    archive = _write_archive(tmp_path / "compression.zip", owner_id=owner_id)
    with ZipFile(archive) as source:
        payloads = {
            info.filename: source.read(info)
            for info in source.infolist()
        }
    with ZipFile(archive, "w", ZIP_BZIP2) as output:
        for name, payload in payloads.items():
            output.writestr(name, payload)

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_size", 99),
        ("chunk_overlap", 1001),
        ("top_k_results", 0),
        ("similarity_threshold", float("nan")),
        ("temperature", 1.1),
        ("max_tokens", 4001),
    ],
)
def test_preview_rejects_invalid_preference_domains(
    importer: BackupImporter,
    tmp_path: Path,
    owner_id: UUID,
    field: str,
    value: Any,
) -> None:
    records = _records()
    records["user_preferences"] = [
        {
            "llm_provider": "openai",
            "llm_model": "model",
            "embedding_model": "embedding",
            "chunk_size": 1000,
            "chunk_overlap": 200,
            "top_k_results": 5,
            "similarity_threshold": 0.7,
            "temperature": 0.7,
            "max_tokens": 1000,
            "theme": "light",
            "language": "en",
            "notes_view_mode": "grid",
            "default_note_folder_id": None,
            "email_notifications": True,
            "processing_notifications": True,
            "rag_diagnostics_enabled": False,
            "created_at": "2026-07-24T12:00:00Z",
            "updated_at": "2026-07-24T12:00:00Z",
        }
    ]
    records["user_preferences"][0][field] = value
    archive = _write_archive(
        tmp_path / f"invalid-{field}.zip",
        owner_id=owner_id,
        records=records,
    )

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


@pytest.mark.parametrize("name", ["x" * 101, "bad\x00tag"])
def test_preview_rejects_strings_invalid_for_durable_schema(
    tmp_path: Path, owner_id: UUID, name: str
) -> None:
    records = _records()
    records["tags"] = [
        {
            "id": str(uuid4()),
            "name": name,
            "color": None,
            "description": None,
            "created_at": "2026-07-24T12:00:00Z",
        }
    ]
    archive = _write_archive(
        tmp_path / "invalid-string.zip",
        owner_id=owner_id,
        records=records,
    )
    importer = BackupImporter(supported_app_versions={"0.1.0"})

    with pytest.raises(UnsafeBackupArchive):
        importer.preview(archive, expected_workspace_owner_id=owner_id)


def test_preview_enforces_each_archive_cardinality_and_size_limit(
    tmp_path: Path, owner_id: UUID
) -> None:
    archive = _write_archive(tmp_path / "all-limits.zip", owner_id=owner_id)
    with ZipFile(archive) as source:
        infos = source.infolist()
    total_expanded = sum(info.file_size for info in infos)
    largest_entry = max(info.file_size for info in infos)

    importers = [
        BackupImporter(
            maximum_entry_count=len(infos) - 1,
            supported_app_versions={"0.1.0"},
        ),
        BackupImporter(
            maximum_entry_size=largest_entry - 1,
            supported_app_versions={"0.1.0"},
        ),
        BackupImporter(
            maximum_expanded_size=total_expanded - 1,
            supported_app_versions={"0.1.0"},
        ),
    ]

    for importer in importers:
        with pytest.raises(UnsafeBackupArchive):
            importer.preview(archive, expected_workspace_owner_id=owner_id)
