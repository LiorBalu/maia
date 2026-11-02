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
    docs_collection_name,
    get_qdrant_client,
)
from maia.api.security import verify_token_extended
from maia.utils.google_cloud_storage.public import upload_to_gcs

logger = logging.getLogger(__name__)

# Router for EmbeddedDocuments (metadata only, no parts)
embedded_documents_router = APIRouter(prefix="/embedded_documents", tags=["Embedded Documents"])

# Router for EmbeddedParts (separate endpoint for better performance)
embedded_parts_router = APIRouter(prefix="/embedded_parts", tags=["Embedded Parts"])


def _parse_categories_form_value(categories: Optional[str]) -> list[str]:
    """Parse multipart `categories` (comma-separated). Handles spaces after commas from the UI."""
    if categories is None:
        return []
    s = str(categories).strip()
    if not s:
        return []
    return [c.strip() for c in s.split(",") if c.strip()]


def _dedupe_document_categories(raw: list[str]) -> list[str]:
    """Strip whitespace and dedupe; preserve original casing."""
    seen: set[str] = set()
    out: list[str] = []
    for c in raw:
        key = (c or "").strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


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
    categories: list[str] = Field(default_factory=list)


@embedded_documents_router.get("/", response_model=list[EmbeddedDocumentListResponse])
async def get_embedded_documents(tenant_name: str, user: dict = Security(verify_token_extended, scopes=["staff"])):
    """Get all embedded documents (metadata only, without parts for better performance)"""
    documents = EmbeddedDocument.objects(tenant_name=tenant_name).order_by("-created_at")
    return [
        EmbeddedDocumentListResponse(
            id=str(doc.id),
            tenant_name=doc.tenant_name,
            title=doc.title,
            content=doc.content,
            url=doc.url or "",
            total_parts=doc.total_parts,
            is_indexed=doc.is_indexed,
            created_at=doc.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=doc.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            categories=list(doc.categories) if doc.categories else [],
        )
        for doc in documents
    ]


@embedded_documents_router.get("/{document_id}/progress", response_model=dict)
async def get_document_indexing_progress(
    document_id: str, user: dict = Security(verify_token_extended, scopes=["staff"])
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
    from maia.knowledge.ingest import get_embedding_progress

    try:
        progress = get_embedding_progress(document_id)
        return progress
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting indexing progress: {str(e)}")


@embedded_parts_router.get("/", response_model=list[EmbeddedPartResponse])
async def get_embedded_parts(
    document_id: str, page: int = 1, page_size: int = 50, user: dict = Security(verify_token_extended, scopes=["staff"])
):
    """Get parts for a specific document with pagination"""
    # Verify document exists
    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Embedded document not found.")

    # Get paginated parts
    skip = (page - 1) * page_size
    parts = EmbeddedPart.objects(document_id=document_id).order_by("part_number").skip(skip).limit(page_size)

    return [
        EmbeddedPartResponse(
            id=str(part.id),
            part_number=part.part_number,
            content=part.content,
            vector_id=part.vector_id,
            vector_for_backup=None,  # Don't send vectors in list view for performance
        )
        for part in parts
    ]


@embedded_documents_router.post("/", response_model=EmbeddedDocumentListResponse)
async def create_embedded_document(
    tenant_name: str = Form(..., description="Tenant name for the document"),
    file: UploadFile = File(..., description="File to upload (PDF, DOC, DOCX, TXT, XLS, XLSX, CSV)"),
    title: Optional[str] = Form(None, description="Optional title for the document"),
    categories: Optional[str] = Form(None, description="Comma-separated list of categories"),
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Upload and create an embedded document.

    The file will be uploaded to GCS and then split into parts for embedding.
    Accepted file types: PDF, DOCX, TXT, XLS, XLSX, CSV
    """
    allowed_types = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    }
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, TXT, XLS, XLSX, or CSV files are allowed")
    #  add suffix if title does not have file extension
    if title and "." not in title:
        file_extension = file.filename.split(".")[-1]
        title += f".{file_extension}"

    # Upload the file to GCS
    uploaded_file_url = upload_to_gcs(
        file_obj=file.file,
        folder=f"embedded_documents/{tenant_name}",
        destination_blob_name=title or file.filename,
        content_type=file.content_type,
    )

    categories_list = _dedupe_document_categories(_parse_categories_form_value(categories))

    doc = EmbeddedDocument(
        tenant_name=tenant_name,
        title=title or file.filename,
        url=uploaded_file_url,
        is_indexed=False,
        total_parts=0,
        categories=categories_list,
    )
    doc.save()

    logger.info(f"Document created: {doc.id} - {doc.title}")

    from background_tasks import index_document_task

    # Call the function directly with error handling
    try:
        logger.info(f"Starting processing for document {doc.id}")
        index_document_task.delay(str(doc.id))
        logger.info(f"Processing completed for document {doc.id}")
    except Exception as e:
        logger.error(f"Error processing document {doc.id}: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to process document: {str(e)}")

    return EmbeddedDocumentListResponse(
        id=str(doc.id),
        tenant_name=doc.tenant_name,
        title=doc.title,
        content=doc.content,
        url=doc.url,
        total_parts=doc.total_parts,
        is_indexed=doc.is_indexed,
        created_at=doc.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=doc.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        categories=list(doc.categories) if doc.categories else [],
    )


@embedded_documents_router.post("/scrape_url", response_model=EmbeddedDocumentListResponse)
async def scrape_and_create_document(
    tenant_name: str = Form(..., description="Tenant name for the document"),
    url: str = Form(..., description="URL of the website to scrape"),
    title: Optional[str] = Form(None, description="Optional title for the document"),
    categories: Optional[str] = Form(None, description="Comma-separated list of categories"),
    max_depth: int = Form(3, description="Maximum depth for recursive scraping (1=single page, 2=page+links, etc.)"),
    max_pages: int = Form(500, description="Maximum number of pages to scrape (prevents infinite crawling)"),
    same_domain_only: bool = Form(True, description="Only scrape pages from the same domain (recommended: True)"),
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Scrape a website URL recursively and create an embedded document.

    The scraping and embedding run in the background via Celery.
    The endpoint returns immediately with the created document (is_indexed=False).
    Use GET /{document_id}/progress to track indexing status.
    """
    # Validate URL format
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    if max_depth < 1 or max_depth > 10:
        raise HTTPException(status_code=400, detail="max_depth must be between 1 and 10")

    if max_pages < 1 or max_pages > 500:
        raise HTTPException(status_code=400, detail="max_pages must be between 1 and 500")

    doc_title = title or f"Web: {url}"

    categories_list = _dedupe_document_categories(_parse_categories_form_value(categories))

    doc = EmbeddedDocument(
        tenant_name=tenant_name,
        title=doc_title,
        url=url,
        content="",
        is_indexed=False,
        total_parts=0,
        categories=categories_list,
    )
    doc.save()

    logger.info(f"Document created from URL: {doc.id} - {doc.title}")

    from background_tasks import index_url_task

    try:
        index_url_task.delay(str(doc.id), url, max_depth, max_pages, same_domain_only)
        logger.info(f"Scrape+embed task dispatched for document {doc.id}")
    except Exception as e:
        logger.error(f"Error dispatching scrape task for document {doc.id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to schedule scraping: {str(e)}")

    return EmbeddedDocumentListResponse(
        id=str(doc.id),
        tenant_name=doc.tenant_name,
        title=doc.title,
        content="",
        url=doc.url,
        total_parts=0,
        is_indexed=False,
        created_at=doc.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=doc.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        categories=list(doc.categories) if doc.categories else [],
    )


@embedded_documents_router.delete("/{document_id}", response_model=dict)
async def delete_embedded_document(document_id: str, user: dict = Security(verify_token_extended, scopes=["staff"])):
    """Delete an embedded document, all its parts, and the Qdrant collection"""
    document = EmbeddedDocument.objects(id=document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Embedded document not found.")

    # Shared per-tenant collection: delete only THIS document's points by
    # payload filter. The collection is never dropped (it holds every other
    # document in the tenant).
    collection_name = docs_collection_name(document.tenant_name)

    qdrant_deleted = False
    qdrant_error = None
    qdrant_client = get_qdrant_client()
    if qdrant_client:
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            qdrant_client.delete(
                collection_name=collection_name,
                points_selector=Filter(
                    must=[FieldCondition(key="document_id", match=MatchValue(value=str(document_id)))]
                ),
            )
            qdrant_deleted = True
        except Exception as e:
            qdrant_error = str(e)
            logger.error(f"Warning: Could not delete points for document '{document_id}' in '{collection_name}': {e}")

    # Delete all parts associated with this document
    parts_deleted = EmbeddedPart.objects(document_id=document_id).delete()

    # Delete the document
    document.delete()

    response = {
        "detail": "Embedded document deleted successfully.",
        "document_id": document_id,
        "mongodb_parts_deleted": parts_deleted,
        "qdrant_collection_deleted": qdrant_deleted,
        "qdrant_collection_name": collection_name,
    }

    if qdrant_error:
        response["qdrant_warning"] = qdrant_error

    return response
