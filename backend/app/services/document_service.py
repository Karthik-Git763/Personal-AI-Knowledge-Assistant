from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import delete, func
from sqlmodel import Session, col, or_, select

from app.ai.document_summary import generate_document_summary
from app.ai.embeddings import generate_embeddings
from app.ai.vectorstore import PgVectorStore
from app.core.config import settings
from app.core.exceptions import AIServiceUnavailableError
from app.models.document import Document, DocumentChunks
from app.models.user import User
from app.schemas.document import DocumentUpdate
from app.utils.file_processing import extract_text_from_file
from app.utils.file_validation import validate_upload_file
from app.utils.text_processing import clean_text, create_content_preview, split_text_into_chunks

logger = logging.getLogger(__name__)


def _remove_uploaded_file(file_path: str) -> None:
    try:
        Path(file_path).unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove uploaded file", extra={"file_path": file_path})


def _mark_document_failed(*, session: Session, document_id: int | None, reason: str) -> None:
    try:
        session.rollback()
    except Exception:
        pass

    if document_id is None:
        return

    try:
        document = session.get(Document, document_id)
        if document is None:
            return

        document.status = "failed"
        document.processing_error = reason
        document.processing_completed_at = datetime.now(UTC)
        session.add(document)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(
            "Failed to persist document failure state",
            extra={"document_id": document_id},
        )


async def upload_and_process_document(
    *,
    session: Session,
    current_user: User,
    file: UploadFile,
    title: str | None = None,
    tags: list[str] | None = None,
    language: str = "en",
) -> Document:
    await _validate_and_prepare(file)
    if current_user.id is None:
        raise HTTPException(status_code=400, detail="Invalid user context")

    saved_path = await _save_upload(file)
    file_size = Path(saved_path).stat().st_size

    document = Document(
        user_id=current_user.id,
        title=title or file.filename or "Untitled",
        file_name=file.filename or Path(saved_path).name,
        file_path=saved_path,
        file_size=file_size,
        file_type=Path(saved_path).suffix.lower(),
        mime_type=file.content_type or "application/octet-stream",
        tags=tags or [],
        language=language,
        status="processing",
    )
    session.add(document)
    session.commit()
    session.refresh(document)

    try:
        if document.id is None:
            raise RuntimeError("Uploaded document was not persisted")
        return await process_existing_document(
            session=session,
            user_id=current_user.id,
            document_id=document.id,
        )
    except HTTPException:
        _remove_uploaded_file(saved_path)
        raise
    except Exception:
        logger.exception(
            "Document processing failed",
            extra={"document_id": document.id, "user_id": current_user.id},
        )
        _remove_uploaded_file(saved_path)
        raise HTTPException(status_code=500, detail="Failed to process document")


async def process_existing_document(
    session: Session,
    user_id: int,
    document_id: int,
) -> Document:
    document = session.get(Document, document_id)
    if document is None or document.user_id != user_id:
        raise ValueError("Document is unavailable for processing")
    if document.is_deleted:
        raise ValueError("Deleted documents cannot be processed")

    document.status = "processing"
    document.processing_started_at = datetime.now(UTC)
    document.processing_completed_at = None
    document.processing_error = None
    document.content = None
    document.content_preview = None
    document.summary = None
    document.keywords = []
    document.word_count = None
    document.page_count = None
    document.chunk_count = 0
    session.exec(delete(DocumentChunks).where(col(DocumentChunks.document_id) == document_id))
    session.add(document)
    session.commit()

    try:
        content = clean_text(extract_text_from_file(document.file_path))
        chunks = split_text_into_chunks(content)
        document.content = content
        document.content_preview = create_content_preview(content)
        document.word_count = len(content.split()) if content else 0
        document.chunk_count = len(chunks)

        chunk_rows: list[DocumentChunks] = []
        for index, chunk_content in enumerate(chunks):
            chunk_row = DocumentChunks(
                document_id=document.id,
                chunk_index=index,
                content=chunk_content,
                content_hash=hashlib.sha256(chunk_content.encode("utf-8")).hexdigest(),
                vector_id=f"doc-{document.id}-chunk-{index}",
                token_count=len(chunk_content.split()),
                char_count=len(chunk_content),
            )
            session.add(chunk_row)
            chunk_rows.append(chunk_row)
        session.flush()

        if chunk_rows:
            try:
                embeddings, embedding_model = await generate_embeddings(
                    session=session,
                    user_id=user_id,
                    texts=[chunk.content for chunk in chunk_rows],
                )
                PgVectorStore(session=session).store_document_chunk_embeddings(
                    chunks=chunk_rows,
                    embeddings=embeddings,
                    user_id=user_id,
                    model=embedding_model,
                )
            except Exception:
                logger.exception(
                    "Failed to generate document embeddings; content remains available",
                    extra={"document_id": document.id, "user_id": user_id},
                )

        if content:
            try:
                document.summary = await generate_document_summary(
                    session=session,
                    user_id=user_id,
                    title=document.title,
                    content=content,
                )
            except Exception:
                document.summary = None
                logger.exception(
                    "Failed to generate document summary; content remains available",
                    extra={"document_id": document.id, "user_id": user_id},
                )

        document.status = "completed"
        document.processing_completed_at = datetime.now(UTC)
        session.add(document)
        session.commit()
        session.refresh(document)
        return document
    except Exception:
        logger.exception(
            "Document processing failed",
            extra={"document_id": document_id, "user_id": user_id},
        )
        _mark_document_failed(
            session=session,
            document_id=document_id,
            reason="Unexpected server error during document processing",
        )
        raise


async def _validate_and_prepare(file: UploadFile) -> None:
    await validate_upload_file(file)


async def _save_upload(file: UploadFile) -> str:
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "").suffix.lower()
    unique_name = f"{uuid.uuid4().hex}{ext}"
    destination = upload_dir / unique_name

    total_size = 0
    try:
        with open(destination, "wb") as output:
            while chunk := await file.read(settings.UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > settings.MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "File too large. Maximum size: "
                            f"{settings.MAX_FILE_SIZE // (1024 * 1024)} MB"
                        ),
                    )
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.seek(0)

    return str(destination)


def list_documents(
    *,
    session: Session,
    current_user: User,
    search: str | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[list[Document], int]:
    """
    List documents with efficient pagination.

    Performance improvements:
    - Uses SQL COUNT(*) for efficient counting (not fetching all rows)
    - Uses database LIMIT/OFFSET instead of in-memory slicing
    """
    base_where = [
        Document.user_id == current_user.id,
        col(Document.is_deleted).is_(False),
    ]

    if search:
        like_query = f"%{search}%"
        base_where.append(
            or_(
                col(Document.title).ilike(like_query),
                col(Document.content).ilike(like_query),
            )
        )

    # Get total count using efficient SQL COUNT
    count_statement = select(func.count()).select_from(Document).where(*base_where)
    total_count = session.exec(count_statement).one() or 0

    # Fetch documents with pagination
    statement = select(Document).where(*base_where).order_by(col(Document.updated_at).desc())
    statement = statement.limit(limit).offset(skip)

    data = session.exec(statement).all()
    return list(data), total_count


def get_document_by_id(*, session: Session, current_user: User, document_id: int) -> Document:
    document = session.exec(
        select(Document).where(
            Document.id == document_id,
            Document.user_id == current_user.id,
            col(Document.is_deleted).is_(False),
        )
    ).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


def update_document_metadata(
    *, session: Session, current_user: User, document_id: int, payload: DocumentUpdate
) -> Document:
    document = get_document_by_id(
        session=session, current_user=current_user, document_id=document_id
    )

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(document, key, value)

    session.add(document)
    session.commit()
    session.refresh(document)
    return document


async def regenerate_document_summary(
    *, session: Session, current_user: User, document_id: int
) -> Document:
    document = get_document_by_id(
        session=session, current_user=current_user, document_id=document_id
    )
    if current_user.id is None:
        raise HTTPException(status_code=400, detail="Invalid user context")
    if not document.content:
        raise HTTPException(status_code=409, detail="Document content is not available")

    try:
        document.summary = await generate_document_summary(
            session=session,
            user_id=current_user.id,
            title=document.title,
            content=document.content,
        )
    except Exception as exc:
        logger.exception(
            "Failed to regenerate document summary",
            extra={"document_id": document.id, "user_id": current_user.id},
        )
        raise AIServiceUnavailableError(
            "The local model could not generate this document summary."
        ) from exc

    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def soft_delete_document(*, session: Session, current_user: User, document_id: int) -> None:
    document = get_document_by_id(
        session=session, current_user=current_user, document_id=document_id
    )
    document.is_deleted = True
    document.status = "deleted"

    file_path = Path(document.file_path)
    if file_path.exists() and file_path.is_file():
        file_path.unlink(missing_ok=True)

    session.add(document)
    session.commit()
