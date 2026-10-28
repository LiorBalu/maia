"""
Q&A (Questions & Answers) API Routes

This module provides endpoints for managing Q&A documents in the RAG system.
Q&A are embedded using Cohere and stored in Qdrant for semantic search.

Endpoints (all called by the dashboard's knowledge page, Q&A tab):
1. POST /qa/ - Create a single Q&A
2. POST /qa/bulk - Create many Q&A from a JSON list
3. GET /qa/ - List all Q&A for a tenant (with pagination)
4. GET /qa/count - Total / indexed / pending counts for a tenant
5. GET /qa/categories - Categories of a tenant with counts
6. PUT /qa/{qa_id} - Update a Q&A (re-embedded)
7. DELETE /qa/{qa_id} - Delete a Q&A (Mongo + Qdrant)

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
    normalize_qa_category,
)
from maia.api.security import verify_token_extended
from maia.api.qa_backfill import ensure_qa_categories_backfilled

logger = logging.getLogger(__name__)


qa_router = APIRouter(prefix="/qa", tags=["Q&A"])


@qa_router.post("/", response_model=QADocumentResponse)
async def create_qa(
    qa_data: QADocumentCreate,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
    tenant_name: Optional[str] = Query(None),
):
    """
    Create a single Q&A document and embed it in Qdrant (the dashboard's "add question" form).

    The tenant comes from the query string or the body; the Q&A is embedded right away.
    """
    tenant = qa_data.tenant_name or tenant_name
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant_name must be provided in query or body")

    qa = QADocument(
        tenant_name=tenant,
        question=qa_data.question,
        answer=qa_data.answer,
        category=normalize_qa_category(qa_data.category),
        metadata=qa_data.metadata,
        is_indexed=False,
    )
    qa.save()
    logger.info(f"Q&A created: {qa.id} in tenant '{tenant}'")

    try:
        from maia.knowledge.qa import create_qa_embedding

        result = create_qa_embedding(str(qa.id))
        logger.info(f"Q&A embedded: {result}")
    except Exception as e:
        # The Q&A is saved but stays pending (is_indexed=False); the dashboard shows it as pending.
        logger.error(f"Error embedding Q&A {qa.id}: {e}")

    qa.reload()

    return QADocumentResponse(
        id=str(qa.id),
        tenant_name=qa.tenant_name,
        question=qa.question,
        answer=qa.answer,
        category=normalize_qa_category(getattr(qa, "category", None)),
        is_indexed=qa.is_indexed,
        metadata=qa.metadata,
        vector_id=qa.vector_id,
        created_at=qa.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=qa.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


@qa_router.post("/bulk", response_model=dict)
async def create_qa_bulk(
    qa_list: List[QADocumentCreate],
    user: dict = Security(verify_token_extended, scopes=["staff"]),
    tenant_name: str = Query(...),
):
    """
    Create multiple Q&A documents in bulk from a JSON list.

    Each Q&A will be automatically embedded and added to the tenant's Q&A collection.

    Args:
        qa_list: List of Q&A documents to create

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
    if not qa_list:
        raise HTTPException(status_code=400, detail="Q&A list cannot be empty")

    created_ids = []
    failed_items = []

    logger.info(f"Starting bulk Q&A creation: {len(qa_list)} items")

    for idx, qa_data in enumerate(qa_list):
        try:
            # Create Q&A document in MongoDB
            qa = QADocument(
                tenant_name=tenant_name,
                question=qa_data.question,
                answer=qa_data.answer,
                category=normalize_qa_category(qa_data.category),
                metadata=qa_data.metadata,
                is_indexed=False,
            )
            qa.save()

            created_ids.append(str(qa.id))
            logger.info(f"  [{idx+1}/{len(qa_list)}] Created Q&A: {qa.id} in tenant '{qa_data.tenant_name}'")

        except Exception as e:
            error_msg = f"Error creating Q&A at index {idx}: {str(e)}"
            failed_items.append(
                {
                    "index": idx,
                    "question": (
                        qa_data.question[:50] + "..."
                        if len(qa_data.question) > 50
                        else qa_data.question
                    ),
                    "error": str(e),
                }
            )
            logger.error(f"  [{idx+1}/{len(qa_list)}] {error_msg}")

    # Now embed all created Q&A
    embedded_count = 0
    embedding_errors = []

    if created_ids:
        logger.info(f"Starting embeddings for {len(created_ids)} Q&A...")
        from maia.knowledge.qa import create_qa_embedding

        for qa_id in created_ids:
            try:
                create_qa_embedding(qa_id)
                embedded_count += 1
                logger.info(f"  Embedded Q&A: {qa_id}")
            except Exception as e:
                embedding_errors.append({"qa_id": qa_id, "error": str(e)})
                logger.error(f"  Error embedding Q&A {qa_id}: {e}")

    logger.info(f"Bulk Q&A creation completed: {len(created_ids)} created, {embedded_count} embedded")

    return {
        "total_items": len(qa_list),
        "created_count": len(created_ids),
        "failed_count": len(failed_items),
        "embedded_count": embedded_count,
        "embedding_errors_count": len(embedding_errors),
        "created_ids": created_ids,
        "failed_items": failed_items if failed_items else None,
        "embedding_errors": embedding_errors if embedding_errors else None,
    }


@qa_router.get("/", response_model=List[QADocumentResponse])
async def get_qa_list(
    tenant_name: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    is_indexed: Optional[bool] = None,
    category: Optional[str] = None,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Get all Q&A documents for a tenant with pagination.

    Args:
        tenant_name: Name of the tenant
        page: Page number (default: 1)
        page_size: Number of items per page (default: 50, max: 100)
        is_indexed: Filter by indexing status (optional)
    """
    ensure_qa_categories_backfilled(tenant_name)

    # Build query
    query_filter = {"tenant_name": tenant_name}
    if is_indexed is not None:
        query_filter["is_indexed"] = is_indexed
    if category is not None:
        query_filter["category"] = normalize_qa_category(category)

    # Get paginated results
    skip = (page - 1) * page_size
    qa_documents = (
        QADocument.objects(**query_filter)
        .order_by("-created_at")
        .skip(skip)
        .limit(page_size)
    )

    return [
        QADocumentResponse(
            id=str(qa.id),
            tenant_name=qa.tenant_name,
            question=qa.question,
            answer=qa.answer,
            category=normalize_qa_category(getattr(qa, "category", None)),
            is_indexed=qa.is_indexed,
            metadata=qa.metadata,
            vector_id=qa.vector_id,
            created_at=qa.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=qa.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        for qa in qa_documents
    ]


@qa_router.get("/count", response_model=dict)
async def get_qa_count(
    tenant_name: str,
    is_indexed: Optional[bool] = None,
    category: Optional[str] = None,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Get total count of Q&A documents for a tenant.

    Args:
        tenant_name: Name of the tenant
        is_indexed: Filter by indexing status (optional)
    """
    ensure_qa_categories_backfilled(tenant_name)

    query_filter = {"tenant_name": tenant_name}
    if is_indexed is not None:
        query_filter["is_indexed"] = is_indexed
    if category is not None:
        query_filter["category"] = normalize_qa_category(category)

    total_count = QADocument.objects(**query_filter).count()
    indexed_query = {"tenant_name": tenant_name, "is_indexed": True}
    if category is not None:
        indexed_query["category"] = normalize_qa_category(category)
    indexed_count = QADocument.objects(**indexed_query).count()

    return {
        "tenant_name": tenant_name,
        "total_count": total_count,
        "indexed_count": indexed_count,
        "pending_count": total_count - indexed_count,
    }


@qa_router.get("/categories", response_model=List[dict])
async def get_qa_categories(
    tenant_name: str,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Get per-category counts for a tenant.

    Returns:
      [{category, total_count, indexed_count, pending_count}]
    """
    ensure_qa_categories_backfilled(tenant_name)

    coll = QADocument._get_collection()
    pipeline = [
        {"$match": {"tenant_name": tenant_name}},
        {
            "$group": {
                "_id": {"$ifNull": ["$category", "general"]},
                "total_count": {"$sum": 1},
                "indexed_count": {
                    "$sum": {"$cond": [{"$eq": ["$is_indexed", True]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    rows = list(coll.aggregate(pipeline))

    formatted = []
    for row in rows:
        category_name = normalize_qa_category(row.get("_id"))
        total = int(row.get("total_count", 0) or 0)
        indexed = int(row.get("indexed_count", 0) or 0)
        formatted.append(
            {
                "category": category_name,
                "total_count": total,
                "indexed_count": indexed,
                "pending_count": total - indexed,
            }
        )

    if not any(x["category"] == "general" for x in formatted):
        formatted.append(
            {
                "category": "general",
                "total_count": 0,
                "indexed_count": 0,
                "pending_count": 0,
            }
        )

    return formatted


@qa_router.put("/{qa_id}", response_model=QADocumentResponse)
async def update_qa(
    qa_id: str,
    qa_update: QADocumentUpdate,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Update a Q&A document.

    After updating, the Q&A will be re-embedded in Qdrant.
    """
    qa = QADocument.objects(id=qa_id).first()
    if not qa:
        raise HTTPException(status_code=404, detail="Q&A not found")

    # Update fields if provided
    if qa_update.question is not None:
        qa.question = qa_update.question
    if qa_update.answer is not None:
        qa.answer = qa_update.answer
    if getattr(qa_update, "category", None) is not None:
        qa.category = normalize_qa_category(qa_update.category)
    if qa_update.metadata is not None:
        qa.metadata = qa_update.metadata

    # Mark as not indexed (will be re-embedded)
    qa.is_indexed = False
    qa.save()

    logger.info(f"Q&A updated: {qa_id}")

    # Re-embed the Q&A
    try:
        from maia.knowledge.qa import create_qa_embedding

        result = create_qa_embedding(str(qa.id))
        logger.info(f"Q&A re-embedded: {result}")
    except Exception as e:
        logger.error(f"Error re-embedding Q&A: {e}")
        # You can implement async re-embedding with Celery here

    # Reload to get updated status
    qa.reload()

    return QADocumentResponse(
        id=str(qa.id),
        tenant_name=qa.tenant_name,
        question=qa.question,
        answer=qa.answer,
        category=normalize_qa_category(getattr(qa, "category", None)),
        is_indexed=qa.is_indexed,
        metadata=qa.metadata,
        vector_id=qa.vector_id,
        created_at=qa.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        updated_at=qa.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


@qa_router.delete("/{qa_id}", response_model=dict)
async def delete_qa(
    qa_id: str,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Delete a Q&A document from MongoDB and Qdrant.
    """
    qa = QADocument.objects(id=qa_id).first()
    if not qa:
        raise HTTPException(status_code=404, detail="Q&A not found")

    tenant_name = qa.tenant_name

    # Delete from Qdrant
    from maia.knowledge.qa import delete_qa_from_qdrant

    qdrant_result = delete_qa_from_qdrant(qa_id, tenant_name)

    # Delete from MongoDB
    qa.delete()

    logger.info(f"Q&A deleted: {qa_id}")

    return {
        "detail": "Q&A deleted successfully",
        "qa_id": qa_id,
        "tenant_name": tenant_name,
        "qdrant_status": qdrant_result.get("status"),
    }
