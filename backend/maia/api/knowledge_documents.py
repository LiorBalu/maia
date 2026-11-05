"""
Embedded Documents & Parts API Routes

This module provides two separate routers for optimized performance:

1. /embedded_documents - Document metadata (fast, lightweight)
   - GET / - List all documents in a tenant
   - POST / - Upload and create new document
   - DELETE /{document_id} - Delete document and all parts

2. /embedded_parts - Document parts/content (paginated)
   - GET / - Get parts with pagination

Cohere Embedding:
- Model: Cohere embed-v4.0 (1024 dimensions)
- Qdrant collections use int8 scalar quantization for efficiency
"""

import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Security, UploadFile
from pydantic import BaseModel, Field

from maia.models.knowledge_documents import EmbeddedDocument, EmbeddedPart, EmbeddedPartResponse
from maia.knowledge.qdrant import (
    derive_tenant_documents_collection_name,
    ensure_qdrant_client,
)
from maia.api.security import authorize_current_user
from maia.utils.google_cloud_storage.public import upload_file_to_public_bucket

logger = logging.getLogger(__name__)

# Router for EmbeddedDocuments (metadata only, no parts)
knowledge_documents_router = APIRouter(prefix="/embedded_documents", tags=["Embedded Documents"])

# Router for EmbeddedParts (separate endpoint for better performance)
document_parts_router = APIRouter(prefix="/embedded_parts", tags=["Embedded Parts"])

# Content types an uploaded knowledge document may have: PDF, DOCX, TXT, XLS, XLSX, CSV
ALLOWED_UPLOAD_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    }
)


class EmbeddedDocumentListResponse(BaseModel):
    """Lightweight response without embedded_parts for list endpoints"""

    id: str
    tenant_name: str
    title: str
    url: str = ""
    total_parts: int
    is_indexed: bool
    created_at: str
    updated_at: str
    content: Optional[str] = Field(None)


def _build_document_list_response(knowledge_document: EmbeddedDocument) -> EmbeddedDocumentListResponse:
    """The API shape of a knowledge document without its parts, timestamps as 'YYYY-MM-DD HH:MM:SS'."""
    return EmbeddedDocumentListResponse(
        id=str(knowledge_document.id),
        tenant_name=knowledge_document.tenant_name,
        title=knowledge_document.title,
        content=knowledge_document.content,
        url=knowledge_document.url or "",
        total_parts=knowledge_document.total_parts,
        is_indexed=knowledge_document.is_indexed,
        created_at=knowledge_document.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=knowledge_document.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


@knowledge_documents_router.get("/", response_model=list[EmbeddedDocumentListResponse])
async def list_knowledge_documents(tenant_name: str, current_user: dict = Security(authorize_current_user, scopes=["staff"])):
    """Get all embedded documents (metadata only, without parts for better performance)"""
    knowledge_documents = EmbeddedDocument.objects(tenant_name=tenant_name).order_by("-created_at")
    return [_build_document_list_response(knowledge_document) for knowledge_document in knowledge_documents]


@knowledge_documents_router.get("/{document_id}/progress", response_model=dict)
async def fetch_knowledge_document_indexing_progress(
    document_id: str, current_user: dict = Security(authorize_current_user, scopes=["staff"])
):
    """
    Get the indexing progress for a document.

    Returns:
        - total_parts: Total number of parts to index
        - completed_parts: Number of parts with embeddings created
        - progress_percentage: Percentage of completion
        - is_indexed: Whether the document is fully indexed
        - status: 'completed', 'processing', or 'pending'
    """
    from maia.knowledge.ingest import compute_indexing_progress

    try:
        indexing_progress = compute_indexing_progress(document_id)
        return indexing_progress
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Error getting indexing progress: {str(error)}")


def _load_knowledge_document_or_404(document_id: str) -> EmbeddedDocument:
    """Load the knowledge document by id or raise 404 'Embedded document not found.'."""
    knowledge_document = EmbeddedDocument.objects(id=document_id).first()
    if not knowledge_document:
        raise HTTPException(status_code=404, detail="Embedded document not found.")
    return knowledge_document


@document_parts_router.get("/", response_model=list[EmbeddedPartResponse])
async def list_document_parts(
    document_id: str, page: int = 1, page_size: int = 50, current_user: dict = Security(authorize_current_user, scopes=["staff"])
):
    """Get parts for a specific document with pagination"""
    # Verify document exists
    _load_knowledge_document_or_404(document_id)

    # Get paginated parts
    skip_count = (page - 1) * page_size
    page_parts = EmbeddedPart.objects(document_id=document_id).order_by("part_number").skip(skip_count).limit(page_size)

    return [
        EmbeddedPartResponse(
            id=str(part.id),
            part_number=part.part_number,
            content=part.content,
            vector_id=part.vector_id,
            vector_for_backup=None,  # Don't send vectors in list view for performance
        )
        for part in page_parts
    ]


def _verify_upload_content_type(uploaded_file: UploadFile) -> None:
    """Raise 400 unless the declared content type is PDF, DOCX, TXT, XLS, XLSX or CSV."""
    if uploaded_file.content_type not in ALLOWED_UPLOAD_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, TXT, XLS, XLSX, or CSV files are allowed")


def _resolve_upload_title(title: Optional[str], uploaded_file: UploadFile) -> str:
    """The title of the uploaded document: the given title with the file's extension appended when it has none, or
    the file name when no title was given. The GCS blob and the Mongo document both take this value.
    """
    #  add suffix if title does not have file extension
    if title and "." not in title:
        filename_extension = uploaded_file.filename.split(".")[-1]
        title += f".{filename_extension}"
    return title or uploaded_file.filename


def _schedule_file_indexing(knowledge_document: EmbeddedDocument) -> None:
    """Queue the Celery task that splits the uploaded file into parts and indexes them; a failed dispatch is a 500."""
    from background_tasks import index_knowledge_document_task

    # Call the function directly with error handling
    try:
        logger.info(f"Starting processing for document {knowledge_document.id}")
        index_knowledge_document_task.delay(str(knowledge_document.id))
        logger.info(f"Processing completed for document {knowledge_document.id}")
    except Exception as error:
        logger.error(f"Error processing document {knowledge_document.id}: {error}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to process document: {str(error)}")


@knowledge_documents_router.post("/", response_model=EmbeddedDocumentListResponse)
async def upload_knowledge_document(
    tenant_name: str = Form(..., description="Tenant name for the document"),
    file: UploadFile = File(..., description="File to upload (PDF, DOC, DOCX, TXT, XLS, XLSX, CSV)"),
    title: Optional[str] = Form(None, description="Optional title for the document"),
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Upload and create an embedded document.

    The file will be uploaded to GCS and then split into parts for embedding.
    Accepted file types: PDF, DOCX, TXT, XLS, XLSX, CSV
    """
    _verify_upload_content_type(file)
    document_title = _resolve_upload_title(title, file)

    # Upload the file to GCS
    gcs_file_url = upload_file_to_public_bucket(
        file_stream=file.file,
        bucket_folder=f"embedded_documents/{tenant_name}",
        relative_blob_path=document_title,
        content_type=file.content_type,
    )

    knowledge_document = EmbeddedDocument(
        tenant_name=tenant_name,
        title=document_title,
        url=gcs_file_url,
        is_indexed=False,
        total_parts=0,
    )
    knowledge_document.save()

    logger.info(f"Document created: {knowledge_document.id} - {knowledge_document.title}")

    _schedule_file_indexing(knowledge_document)

    return _build_document_list_response(knowledge_document)


def _verify_scrape_parameters(url: str, max_depth: int, max_pages: int) -> None:
    """Raise 400 unless the URL starts with http:// or https://, max_depth is 1-10 and max_pages is 1-500."""
    # Validate URL format
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    if max_depth < 1 or max_depth > 10:
        raise HTTPException(status_code=400, detail="max_depth must be between 1 and 10")

    if max_pages < 1 or max_pages > 500:
        raise HTTPException(status_code=400, detail="max_pages must be between 1 and 500")


def _schedule_url_scraping(
    knowledge_document: EmbeddedDocument, url: str, max_depth: int, max_pages: int, same_domain_only: bool
) -> None:
    """Queue the Celery task that scrapes the site into the document and indexes it; a failed dispatch is a 500."""
    from background_tasks import scrape_and_index_url_task

    try:
        scrape_and_index_url_task.delay(str(knowledge_document.id), url, max_depth, max_pages, same_domain_only)
        logger.info(f"Scrape+embed task dispatched for document {knowledge_document.id}")
    except Exception as error:
        logger.error(f"Error dispatching scrape task for document {knowledge_document.id}: {error}")
        raise HTTPException(status_code=500, detail=f"Failed to schedule scraping: {str(error)}")


@knowledge_documents_router.post("/scrape_url", response_model=EmbeddedDocumentListResponse)
async def create_knowledge_document_from_url(
    tenant_name: str = Form(..., description="Tenant name for the document"),
    url: str = Form(..., description="URL of the website to scrape"),
    title: Optional[str] = Form(None, description="Optional title for the document"),
    max_depth: int = Form(3, description="Maximum depth for recursive scraping (1=single page, 2=page+links, etc.)"),
    max_pages: int = Form(500, description="Maximum number of pages to scrape (prevents infinite crawling)"),
    same_domain_only: bool = Form(True, description="Only scrape pages from the same domain (recommended: True)"),
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Scrape a website URL recursively and create an embedded document.

    The scraping and embedding run in the background via Celery.
    The endpoint returns immediately with the created document (is_indexed=False).
    Use GET /{document_id}/progress to track indexing status.
    """
    _verify_scrape_parameters(url, max_depth, max_pages)

    document_title = title or f"Web: {url}"

    knowledge_document = EmbeddedDocument(
        tenant_name=tenant_name,
        title=document_title,
        url=url,
        content="",
        is_indexed=False,
        total_parts=0,
    )
    knowledge_document.save()

    logger.info(f"Document created from URL: {knowledge_document.id} - {knowledge_document.title}")

    _schedule_url_scraping(knowledge_document, url, max_depth, max_pages, same_domain_only)

    return _build_document_list_response(knowledge_document)


def _remove_knowledge_document_from_index(qdrant_collection_name: str, document_id: str) -> tuple[bool, Optional[str]]:
    """Delete the document's points from the tenant's shared Qdrant collection (payload filter on document_id).

    Returns (has_deleted_vectors, the error text or None). A falsy client deletes nothing and raises nothing; a failed
    delete is logged and returned as the error text, so the caller still deletes the parts and the document.
    """
    has_deleted_vectors = False
    vector_delete_error = None
    qdrant_client = ensure_qdrant_client()
    if qdrant_client:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            qdrant_client.delete(
                collection_name=qdrant_collection_name,
                points_selector=Filter(
                    must=[FieldCondition(key="document_id", match=MatchValue(value=str(document_id)))]
                ),
            )
            has_deleted_vectors = True
        except Exception as error:
            vector_delete_error = str(error)
            logger.error(
                f"Warning: Could not delete points for document '{document_id}' in '{qdrant_collection_name}': {error}"
            )
    return has_deleted_vectors, vector_delete_error


@knowledge_documents_router.delete("/{document_id}", response_model=dict)
async def delete_knowledge_document(document_id: str, current_user: dict = Security(authorize_current_user, scopes=["staff"])):
    """Delete an embedded document, all its parts, and the Qdrant collection"""
    knowledge_document = _load_knowledge_document_or_404(document_id)

    # Shared per-tenant collection: delete only THIS document's points by
    # payload filter. The collection is never dropped (it holds every other
    # document in the tenant).
    qdrant_collection_name = derive_tenant_documents_collection_name(knowledge_document.tenant_name)

    has_deleted_vectors, vector_delete_error = _remove_knowledge_document_from_index(
        qdrant_collection_name, document_id
    )

    # Delete all parts associated with this document
    deleted_parts_count = EmbeddedPart.objects(document_id=document_id).delete()

    # Delete the document
    knowledge_document.delete()

    delete_summary = {
        "detail": "Embedded document deleted successfully.",
        "document_id": document_id,
        "mongodb_parts_deleted": deleted_parts_count,
        "qdrant_collection_deleted": has_deleted_vectors,
        "qdrant_collection_name": qdrant_collection_name,
    }

    if vector_delete_error:
        delete_summary["qdrant_warning"] = vector_delete_error

    return delete_summary
