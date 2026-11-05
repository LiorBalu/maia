"""
Q&A (Questions & Answers) API Routes

This module provides endpoints for managing Q&A documents in the RAG system.
Q&A are embedded using Cohere and stored in Qdrant for semantic search.

Endpoints (all called by the dashboard's knowledge page, Q&A tab):
1. POST /qa/ - Create a single Q&A
2. POST /qa/bulk - Create many Q&A from a JSON list
3. GET /qa/ - List all Q&A for a tenant (with pagination)
4. GET /qa/count - Total / indexed / pending counts for a tenant
5. PUT /qa/{qa_id} - Update a Q&A (re-embedded)
6. DELETE /qa/{qa_id} - Delete a Q&A (Mongo + Qdrant)

Cohere Embedding Strategy:
- EMBED: Question field only (semantic search target)
- PAYLOAD: {"text": answer, "original_question": question, "source": "qa", "mongo_id": id}
- Model: Cohere embed-v4.0 (1024 dimensions)
- Qdrant collection: {tenant_name}_qa (with int8 quantization)
"""

import logging

from fastapi import APIRouter, Security, HTTPException, Query
from typing import Optional, List
from maia.models.qa import (
    QADocument,
    QADocumentResponse,
    QADocumentCreate,
    QADocumentUpdate,
)
from maia.api.security import authorize_current_user

logger = logging.getLogger(__name__)


qa_router = APIRouter(prefix="/qa", tags=["Q&A"])


def _persist_new_qa_pair(tenant_name: str, qa_pair_input: QADocumentCreate) -> QADocument:
    """Save a new Q&A pair of the tenant (question, answer, metadata), not indexed yet, and return it."""
    qa_doc = QADocument(
        tenant_name=tenant_name,
        question=qa_pair_input.question,
        answer=qa_pair_input.answer,
        metadata=qa_pair_input.metadata,
        is_indexed=False,
    )
    qa_doc.save()
    return qa_doc


def _index_qa_pair_or_log_failure(qa_doc_id: str, is_reindexing: bool) -> None:
    """Embed the Q&A pair and upsert it in Qdrant; any failure, the import included, is logged and swallowed.

    is_reindexing only picks the log wording: True after an edit ("re-embedded"), False for a new pair ("embedded").
    """
    try:
        from maia.knowledge.qa import index_qa_pair

        indexing_result = index_qa_pair(qa_doc_id)
        if is_reindexing:
            logger.info(f"Q&A re-embedded: {indexing_result}")
        else:
            logger.info(f"Q&A embedded: {indexing_result}")
    except Exception as error:
        # The Q&A is saved but stays pending (is_indexed=False); the dashboard shows it as pending.
        if is_reindexing:
            logger.error(f"Error re-embedding Q&A: {error}")
        else:
            logger.error(f"Error embedding Q&A {qa_doc_id}: {error}")


def _build_qa_pair_response(qa_doc: QADocument) -> QADocumentResponse:
    """The API shape of a saved Q&A pair, timestamps as 'YYYY-MM-DD HH:MM:SS'."""
    return QADocumentResponse(
        id=str(qa_doc.id),
        tenant_name=qa_doc.tenant_name,
        question=qa_doc.question,
        answer=qa_doc.answer,
        is_indexed=qa_doc.is_indexed,
        metadata=qa_doc.metadata,
        vector_id=qa_doc.vector_id,
        created_at=qa_doc.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=qa_doc.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


@qa_router.post("/", response_model=QADocumentResponse)
async def create_and_index_qa_pair(
    qa_pair_input: QADocumentCreate,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
    tenant_name: Optional[str] = Query(None),
):
    """
    Create a single Q&A document and embed it in Qdrant (the dashboard's "add question" form).

    The tenant comes from the query string or the body; the Q&A is embedded right away.
    """
    resolved_tenant_name = qa_pair_input.tenant_name or tenant_name
    if not resolved_tenant_name:
        raise HTTPException(status_code=400, detail="tenant_name must be provided in query or body")

    qa_doc = _persist_new_qa_pair(resolved_tenant_name, qa_pair_input)
    logger.info(f"Q&A created: {qa_doc.id} in tenant '{resolved_tenant_name}'")

    _index_qa_pair_or_log_failure(str(qa_doc.id), is_reindexing=False)

    qa_doc.reload()

    return _build_qa_pair_response(qa_doc)


def _build_failed_qa_pair_entry(item_index: int, qa_pair_input: QADocumentCreate, error: Exception) -> dict:
    """Summary entry of a pair the bulk upload could not save: its index, its question (cut to 50 characters plus
    '...') and the error text.
    """
    return {
        "index": item_index,
        "question": (
            qa_pair_input.question[:50] + "..."
            if len(qa_pair_input.question) > 50
            else qa_pair_input.question
        ),
        "error": str(error),
    }


def _persist_qa_pairs_in_bulk(
    tenant_name: str, qa_pair_inputs: list[QADocumentCreate]
) -> tuple[list[str], list[dict]]:
    """Save every pair of the bulk upload in order; a pair that fails is logged and reported, the others go on.

    Returns (created_qa_ids, failed_items).
    """
    created_qa_ids = []
    failed_items = []

    for item_index, qa_pair_input in enumerate(qa_pair_inputs):
        try:
            # Create Q&A document in MongoDB
            qa_doc = _persist_new_qa_pair(tenant_name, qa_pair_input)

            created_qa_ids.append(str(qa_doc.id))
            logger.info(
                f"  [{item_index+1}/{len(qa_pair_inputs)}] Created Q&A: {qa_doc.id} "
                f"in tenant '{qa_pair_input.tenant_name}'"
            )

        except Exception as error:
            creation_error_message = f"Error creating Q&A at index {item_index}: {str(error)}"
            failed_items.append(_build_failed_qa_pair_entry(item_index, qa_pair_input, error))
            logger.error(f"  [{item_index+1}/{len(qa_pair_inputs)}] {creation_error_message}")

    return created_qa_ids, failed_items


def _index_created_qa_pairs(created_qa_ids: list[str]) -> tuple[int, list[dict]]:
    """Index the pairs the bulk upload saved, one by one; a failure is logged and reported, the others go on.

    Returns (embedded_count, embedding_errors). Without ids it returns (0, []) at once: no log line and no import of
    maia.knowledge.qa (which loads the Cohere and Qdrant clients).
    """
    if not created_qa_ids:
        return 0, []

    logger.info(f"Starting embeddings for {len(created_qa_ids)} Q&A...")
    from maia.knowledge.qa import index_qa_pair

    embedded_count = 0
    embedding_errors = []
    for created_qa_id in created_qa_ids:
        try:
            index_qa_pair(created_qa_id)
            embedded_count += 1
            logger.info(f"  Embedded Q&A: {created_qa_id}")
        except Exception as error:
            embedding_errors.append({"qa_id": created_qa_id, "error": str(error)})
            logger.error(f"  Error embedding Q&A {created_qa_id}: {error}")

    return embedded_count, embedding_errors


@qa_router.post("/bulk", response_model=dict)
async def create_qa_pairs_in_bulk(
    qa_pair_inputs: List[QADocumentCreate],
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
    tenant_name: str = Query(...),
):
    """
    Create multiple Q&A documents in bulk from a JSON list.

    Each Q&A will be automatically embedded and added to the tenant's Q&A collection.

    Args:
        qa_pair_inputs: List of Q&A documents to create

    Returns:
        Summary of created Q&A with success/failure counts

    Example request body:
    ```json
    [
        {
            "question": "מה עושה מסך ההתחברות?",
            "answer": "זה המסך שמאפשר להתחבר למערכת עם אימייל וסיסמה",
            "metadata": {
                "screen": "Login",
                "file": "src/screens/Login.tsx",
                "tags": ["כניסה", "אימות"]
            }
        }
    ]
    ```
    """
    if not qa_pair_inputs:
        raise HTTPException(status_code=400, detail="Q&A list cannot be empty")

    logger.info(f"Starting bulk Q&A creation: {len(qa_pair_inputs)} items")

    created_qa_ids, failed_items = _persist_qa_pairs_in_bulk(tenant_name, qa_pair_inputs)

    # Now embed all created Q&A
    embedded_count, embedding_errors = _index_created_qa_pairs(created_qa_ids)

    logger.info(f"Bulk Q&A creation completed: {len(created_qa_ids)} created, {embedded_count} embedded")

    return {
        "total_items": len(qa_pair_inputs),
        "created_count": len(created_qa_ids),
        "failed_count": len(failed_items),
        "embedded_count": embedded_count,
        "embedding_errors_count": len(embedding_errors),
        "created_ids": created_qa_ids,
        "failed_items": failed_items if failed_items else None,
        "embedding_errors": embedding_errors if embedding_errors else None,
    }


def _build_qa_pairs_filter(tenant_name: str, is_indexed: Optional[bool]) -> dict:
    """Keyword filter for QADocument.objects: the tenant's pairs, only the indexed or only the pending ones when
    is_indexed is given.
    """
    qa_pairs_filter = {"tenant_name": tenant_name}
    if is_indexed is not None:
        qa_pairs_filter["is_indexed"] = is_indexed
    return qa_pairs_filter


@qa_router.get("/", response_model=List[QADocumentResponse])
async def list_qa_pairs(
    tenant_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    is_indexed: Optional[bool] = None,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Get all Q&A documents for a tenant with pagination.

    Args:
        tenant_name: Name of the tenant
        page: Page number (default: 1)
        page_size: Number of items per page (default: 50, max: 100)
        is_indexed: Filter by indexing status (optional)
    """
    # Build query
    qa_pairs_filter = _build_qa_pairs_filter(tenant_name, is_indexed)

    # Get paginated results
    skip_count = (page - 1) * page_size
    qa_docs_page = (
        QADocument.objects(**qa_pairs_filter)
        .order_by("-created_at")
        .skip(skip_count)
        .limit(page_size)
    )

    return [_build_qa_pair_response(qa_doc) for qa_doc in qa_docs_page]


@qa_router.get("/count", response_model=dict)
async def count_qa_pairs(
    tenant_name: str,
    is_indexed: Optional[bool] = None,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Get total count of Q&A documents for a tenant.

    Args:
        tenant_name: Name of the tenant
        is_indexed: Filter by indexing status (optional)
    """
    qa_pairs_filter = _build_qa_pairs_filter(tenant_name, is_indexed)

    total_count = QADocument.objects(**qa_pairs_filter).count()
    indexed_filter = _build_qa_pairs_filter(tenant_name, is_indexed=True)
    indexed_count = QADocument.objects(**indexed_filter).count()

    return {
        "tenant_name": tenant_name,
        "total_count": total_count,
        "indexed_count": indexed_count,
        "pending_count": total_count - indexed_count,
    }


def _load_qa_pair_or_404(qa_id: str) -> QADocument:
    """Load the Q&A pair by id or raise 404 'Q&A not found'."""
    qa_doc = QADocument.objects(id=qa_id).first()
    if not qa_doc:
        raise HTTPException(status_code=404, detail="Q&A not found")
    return qa_doc


def _apply_qa_pair_changes(qa_doc: QADocument, qa_pair_changes: QADocumentUpdate) -> None:
    """Copy the fields the edit provides onto the pair, mark it not indexed and save it."""
    # Update fields if provided
    if qa_pair_changes.question is not None:
        qa_doc.question = qa_pair_changes.question
    if qa_pair_changes.answer is not None:
        qa_doc.answer = qa_pair_changes.answer
    if qa_pair_changes.metadata is not None:
        qa_doc.metadata = qa_pair_changes.metadata

    # Mark as not indexed (will be re-embedded)
    qa_doc.is_indexed = False
    qa_doc.save()


@qa_router.put("/{qa_id}", response_model=QADocumentResponse)
async def update_and_reindex_qa_pair(
    qa_id: str,
    qa_pair_changes: QADocumentUpdate,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Update a Q&A document.

    After updating, the Q&A will be re-embedded in Qdrant.
    """
    qa_doc = _load_qa_pair_or_404(qa_id)

    _apply_qa_pair_changes(qa_doc, qa_pair_changes)

    logger.info(f"Q&A updated: {qa_id}")

    # Re-embed the Q&A
    _index_qa_pair_or_log_failure(str(qa_doc.id), is_reindexing=True)

    # Reload to get updated status
    qa_doc.reload()

    return _build_qa_pair_response(qa_doc)


@qa_router.delete("/{qa_id}", response_model=dict)
async def delete_qa_pair(
    qa_id: str,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Delete a Q&A document from MongoDB and Qdrant.
    """
    qa_doc = _load_qa_pair_or_404(qa_id)

    qa_pair_tenant_name = qa_doc.tenant_name

    # Delete from Qdrant
    from maia.knowledge.qa import remove_qa_pair_from_index

    index_removal_result = remove_qa_pair_from_index(qa_id, qa_pair_tenant_name)

    # Delete from MongoDB
    qa_doc.delete()

    logger.info(f"Q&A deleted: {qa_id}")

    return {
        "detail": "Q&A deleted successfully",
        "qa_id": qa_id,
        "tenant_name": qa_pair_tenant_name,
        "qdrant_status": index_removal_result.get("status"),
    }
