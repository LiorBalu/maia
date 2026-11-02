"""
Q&A Embedding Creation and Management.

This module handles embedding Q&A documents using Cohere embed-v4.0.

Cohere Embedding Strategy:
- EMBED: The question field only (semantic search target)
- PAYLOAD: {
    "text": answer,              # For reranker/context
    "original_question": question,
    "source": "qa",
    "mongo_id": qa_id,
    "custom_metadata": metadata
  }

Configuration:
- Model: Cohere embed-v4.0 (1024 dimensions)
- Collection: {tenant_name}_qa with int8 quantization
"""

import logging
import hashlib
import os
from typing import Any, Dict, List, Optional

from qdrant_client.models import PointStruct

from maia.knowledge.embeddings import CohereEmbeddingsV4
from maia.models.qa import QADocument
from maia.knowledge.qdrant import collection_exists, create_cohere_collection, get_qdrant_client

logger = logging.getLogger(__name__)


def _mongodb_id_to_point_id(mongodb_id: str) -> int:
    """
    Convert MongoDB ObjectId string to a valid Qdrant point ID (unsigned integer).

    Args:
        mongodb_id: MongoDB ObjectId as string

    Returns:
        Unsigned integer suitable for Qdrant point ID
    """
    hash_value = int(hashlib.md5(mongodb_id.encode()).hexdigest()[:15], 16)
    return hash_value


def _is_image_data_uri(text: str) -> bool:
    """Check if a string is a base64 image data URI."""
    return isinstance(text, str) and text.startswith("data:image/")


def create_qa_embedding(qa_id: str) -> Dict[str, Any]:
    """
    Create embedding for a Q&A document.

    Indexing Strategy:
    - Text questions: EMBED question text with Cohere text embedding
    - Image questions: EMBED image with Cohere multimodal embedding
    - PAYLOAD: {
        "text": answer,              # For reranker/context
        "original_question": question,
        "source": "qa",
        "mongo_id": qa_id,
        "is_image_question": bool    # Whether the question is an image
      }

    Args:
        qa_id: ID of the QADocument

    Returns:
        Dict with status and details
    """
    qa = QADocument.objects(id=qa_id).first()
    if not qa:
        raise ValueError(f"QADocument {qa_id} not found")

    collection_name = f"{qa.tenant_name}_qa"
    client = get_qdrant_client()

    # Create collection if not exists (with Cohere quantization)
    if not collection_exists(client, collection_name):
        create_cohere_collection(client, collection_name)

    # Detect if question is an image data URI
    is_image = _is_image_data_uri(qa.question)

    if is_image:
        # Embed the IMAGE using Cohere multimodal embedding
        embeddings = CohereEmbeddingsV4(input_type="search_document")
        vector = embeddings.embed_image(qa.question)
    else:
        # Embed the QUESTION text only (not combined text)
        embeddings = CohereEmbeddingsV4(input_type="search_document")
        vector = embeddings.embed_query(qa.question)

    # Convert MongoDB ID to Qdrant point ID
    point_id = _mongodb_id_to_point_id(str(qa.id))

    # Store with answer in payload (for reranker)
    point = PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "text": qa.answer,  # Answer for reranker/context
            "original_question": qa.question if not is_image else "[image]",  # Don't store large base64 in payload
            "source": "qa",
            "mongo_id": str(qa.id),
            "category": getattr(qa, "category", "general") or "general",
            "is_image_question": is_image,
            "custom_metadata": qa.metadata or {},
        },
    )

    client.upsert(collection_name=collection_name, points=[point])

    # Update MongoDB document
    qa.vector_id = str(point_id)
    qa.vector_for_backup = vector
    qa.is_indexed = True
    qa.save()

    logger.info(f"Q&A embedded (Cohere): {qa_id} in collection {collection_name}")

    return {
        "status": "success",
        "qa_id": qa_id,
        "collection_name": collection_name,
        "is_indexed": True,
    }


def search_in_qa(
    tenant_name: str,
    query: str,
    top_k: int = 5,
    metadata_filter: Optional[Dict[str, Any]] = None,
    categories: Optional[List[str]] = None,
    query_image: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search in Q&A documents using Cohere embeddings and vector similarity.

    Args:
        tenant_name: Name of the tenant
        query: The search query (text)
        top_k: Number of top results to return
        metadata_filter: Optional filter for metadata
        categories: Optional list of categories to filter by
        query_image: Optional base64 data URI of an image to search by.
                     When provided, the image is embedded and used for vector search
                     instead of the text query (multimodal similarity matching).

    Returns:
        List of relevant Q&A with scores
    """
    collection_name = f"{tenant_name}_qa"
    client = get_qdrant_client()

    # Check if collection exists
    if not collection_exists(client, collection_name):
        logger.warning(f"Collection {collection_name} does not exist")
        return []

    # Create embedding for query using Cohere
    if query_image:
        # Image query: embed the image for multimodal similarity search
        embeddings = CohereEmbeddingsV4(input_type="search_query")
        query_vector = embeddings.embed_image(query_image)
        is_image_query = True
    else:
        # Text query: embed text normally
        embeddings = CohereEmbeddingsV4(input_type="search_query")
        query_vector = embeddings.embed_query(query)
        is_image_query = False

    # Build filter if provided (keep logic close to original)
    qdrant_filter = None
    if metadata_filter or categories:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        conditions = []

        # Category filter (payload.category in categories)
        if categories:
            conditions.append(
                FieldCondition(
                    key="category",
                    match=MatchAny(any=categories),   
                )
            )

        # Dict-based filter matches on custom_metadata.<key> == value
        if metadata_filter:
            for key, value in metadata_filter.items():
                conditions.append(
                    FieldCondition(
                        key=f"custom_metadata.{key}",
                        match=MatchValue(value=value),
                    )
                )

        if conditions:
            qdrant_filter = Filter(must=conditions)

    # Fetch more candidates than top_k, then Cohere-rerank them.
    # Cohere rerank performs best with a reasonable candidate set (<= 200-500).
    candidate_k = max(top_k * 10, 50)
    candidate_k = min(candidate_k, 200)

    # Search in Qdrant (candidate pool)
    results = client.search(
        collection_name=collection_name,
        query_vector=query_vector,
        limit=candidate_k,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    # Format candidates (vector similarity from Qdrant).
    candidates: List[Dict[str, Any]] = []
    for hit in results or []:
        payload = hit.payload or {}
        candidates.append(
            {
                "qa_id": payload.get("mongo_id"),
                "question": payload.get("original_question"),
                "answer": payload.get("text"),
                "metadata": payload.get("custom_metadata", {}),
                "category": payload.get("category") or "general",
                # Qdrant vector similarity (higher is better)
                "score": hit.score,
                "relevance": hit.score,
            }
        )

    if not candidates:
        return []

    # For image queries, skip text-based Cohere rerank (it only works on text).
    # Vector similarity from Cohere embed-v4.0 multimodal tenant is already meaningful.
    if is_image_query:
        return candidates[:top_k]

    # Cohere rerank (best-effort; fallback to vector order on any failure)
    try:
        import cohere  # type: ignore

        cohere_api_key = os.getenv("COHERE_API_KEY")
        if not cohere_api_key:
            return candidates[:top_k]

        rerank_model = os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-pro")

        # Build rerank documents. Include both Q and A for better disambiguation.
        rerank_docs: List[str] = []
        for c in candidates:
            q = (c.get("question") or "").strip()
            a = (c.get("answer") or "").strip()
            rerank_docs.append(f"Question: {q}\nAnswer: {a}".strip())

        co = cohere.ClientV2(api_key=cohere_api_key)
        rr = co.rerank(
            model=rerank_model,
            query=query,
            documents=rerank_docs,
            top_n=min(top_k, len(rerank_docs)),
        )

        # rr.results items: { index, relevance_score }
        reranked: List[Dict[str, Any]] = []
        for item in getattr(rr, "results", []) or []:
            idx = getattr(item, "index", None)
            score = getattr(item, "relevance_score", None)
            if idx is None or score is None:
                continue
            if not (0 <= int(idx) < len(candidates)):
                continue
            c = dict(candidates[int(idx)])
            # Prefer rerank as the single score signal.
            # We intentionally DO NOT keep the Qdrant score in the output payload.
            c["score"] = float(score)
            c["relevance"] = float(score)
            reranked.append(c)

        return reranked if reranked else candidates[:top_k]
    except Exception:
        return candidates[:top_k]


def delete_qa_from_qdrant(qa_id: str, tenant_name: str) -> Dict[str, Any]:
    """
    Delete a Q&A from Qdrant collection.

    Args:
        qa_id: ID of the QADocument
        tenant_name: Name of the tenant

    Returns:
        Dict with status
    """
    collection_name = f"{tenant_name}_qa"
    client = get_qdrant_client()

    try:
        # Convert MongoDB ID to Qdrant point ID
        point_id = _mongodb_id_to_point_id(qa_id)

        client.delete(
            collection_name=collection_name,
            points_selector=[point_id],
        )
        logger.info(f"Deleted Q&A {qa_id} from Qdrant collection {collection_name}")
        return {"status": "success", "qa_id": qa_id, "collection_name": collection_name}
    except Exception as e:
        logger.error(f"Error deleting Q&A from Qdrant: {e}")
        return {"status": "error", "error": str(e)}
