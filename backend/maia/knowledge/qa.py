"""
Q&A Embedding Creation and Management.

This module handles embedding Q&A documents using Cohere embed-v4.0.

Cohere Embedding Strategy:
- EMBED: The question field only (semantic search target)
- PAYLOAD: {
    "text": answer,              # For reranker/context
    "original_question": question,
    "source": "qa",
    "mongo_id": qa_pair_id,
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

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct

from maia.knowledge.embeddings import CohereEmbeddingsV4
from maia.models.qa import QADocument
from maia.knowledge.qdrant import has_collection, create_qa_collection, ensure_qdrant_client

logger = logging.getLogger(__name__)


def _derive_qa_point_id(mongo_object_id: str) -> int:
    """
    Convert MongoDB ObjectId string to a valid Qdrant point ID (unsigned integer).

    Args:
        mongo_object_id: MongoDB ObjectId as string

    Returns:
        Unsigned integer suitable for Qdrant point ID
    """
    point_id = int(hashlib.md5(mongo_object_id.encode()).hexdigest()[:15], 16)
    return point_id


def _is_image_data_url(value: str) -> bool:
    """Check if a string is a base64 image data URI."""
    return isinstance(value, str) and value.startswith("data:image/")


def _qa_collection_name(tenant_name: str) -> str:
    """The tenant's Q&A collection name in Qdrant ("<tenant_name>_qa")."""
    return f"{tenant_name}_qa"


def _embed_question(question: str, question_is_image: bool) -> List[float]:
    """Index vector of a Q&A question: the image itself when the question is an image data URL, otherwise the
    question text only.
    """
    if question_is_image:
        # Embed the IMAGE using Cohere multimodal embedding
        embedder = CohereEmbeddingsV4(input_type="search_document")
        question_vector = embedder.embed_image_from_data_url(question)
    else:
        # Embed the QUESTION text only (not combined text)
        embedder = CohereEmbeddingsV4(input_type="search_document")
        question_vector = embedder.embed_query(question)
    return question_vector


def _build_qa_point(
    qa_doc: QADocument, point_id: int, question_vector: List[float], question_is_image: bool
) -> PointStruct:
    """The Qdrant point of one Q&A pair: its question vector and the payload the reranker and the retriever read
    (answer as text, the question or "[image]", source, mongo_id, is_image_question, custom_metadata).
    """
    # Store with answer in payload (for reranker)
    return PointStruct(
        id=point_id,
        vector=question_vector,
        payload={
            "text": qa_doc.answer,  # Answer for reranker/context
            # Don't store large base64 in payload
            "original_question": qa_doc.question if not question_is_image else "[image]",
            "source": "qa",
            "mongo_id": str(qa_doc.id),
            "is_image_question": question_is_image,
            "custom_metadata": qa_doc.metadata or {},
        },
    )


def _mark_qa_indexed(qa_doc: QADocument, point_id: int, question_vector: List[float]) -> None:
    """Save the index state on the Mongo document: the Qdrant point id, the vector backup and is_indexed."""
    # Update MongoDB document
    qa_doc.vector_id = str(point_id)
    qa_doc.vector_for_backup = question_vector
    qa_doc.is_indexed = True
    qa_doc.save()


def index_qa_pair(qa_pair_id: str) -> Dict[str, Any]:
    """
    Create embedding for a Q&A document.

    Indexing Strategy:
    - Text questions: EMBED question text with Cohere text embedding
    - Image questions: EMBED image with Cohere multimodal embedding
    - PAYLOAD: {
        "text": answer,              # For reranker/context
        "original_question": question,
        "source": "qa",
        "mongo_id": qa_pair_id,
        "is_image_question": bool    # Whether the question is an image
      }

    Args:
        qa_pair_id: ID of the QADocument

    Returns:
        Dict with status and details
    """
    qa_doc = QADocument.objects(id=qa_pair_id).first()
    if not qa_doc:
        raise ValueError(f"QADocument {qa_pair_id} not found")

    collection_name = _qa_collection_name(qa_doc.tenant_name)
    qdrant_client = ensure_qdrant_client()

    # Create collection if not exists (with Cohere quantization)
    if not has_collection(qdrant_client, collection_name):
        create_qa_collection(qdrant_client, collection_name)

    # Detect if question is an image data URI
    question_is_image = _is_image_data_url(qa_doc.question)
    question_vector = _embed_question(qa_doc.question, question_is_image)

    # Convert MongoDB ID to Qdrant point ID
    point_id = _derive_qa_point_id(str(qa_doc.id))
    qa_point = _build_qa_point(qa_doc, point_id, question_vector, question_is_image)

    qdrant_client.upsert(collection_name=collection_name, points=[qa_point])

    _mark_qa_indexed(qa_doc, point_id, question_vector)

    logger.info(f"Q&A embedded (Cohere): {qa_pair_id} in collection {collection_name}")

    return {
        "status": "success",
        "qa_id": qa_pair_id,
        "collection_name": collection_name,
        "is_indexed": True,
    }


def _embed_search_query(query_text: str, query_image: Optional[str]) -> List[float]:
    """Search vector of the query: the image when one is given (multimodal similarity), otherwise the text."""
    if query_image:
        # Image query: embed the image for multimodal similarity search
        embedder = CohereEmbeddingsV4(input_type="search_query")
        query_vector = embedder.embed_image_from_data_url(query_image)
    else:
        # Text query: embed text normally
        embedder = CohereEmbeddingsV4(input_type="search_query")
        query_vector = embedder.embed_query(query_text)
    return query_vector


def _build_qa_filter(required_metadata: Optional[Dict[str, Any]]) -> Optional[Filter]:
    """Qdrant filter of the Q&A search: custom_metadata.<key> == value for every required metadata item; None when
    none was requested.
    """
    # Build filter if provided (keep logic close to original)
    qdrant_filter = None
    if required_metadata:
        filter_conditions = []

        # Dict-based filter matches on custom_metadata.<key> == value
        if required_metadata:
            for metadata_key, metadata_value in required_metadata.items():
                filter_conditions.append(
                    FieldCondition(
                        key=f"custom_metadata.{metadata_key}",
                        match=MatchValue(value=metadata_value),
                    )
                )

        if filter_conditions:
            qdrant_filter = Filter(must=filter_conditions)
    return qdrant_filter


def _qa_hit_to_candidate(hit: Any) -> Dict[str, Any]:
    """One rerank candidate from a Qdrant hit of the Q&A collection: qa_id, question, answer, metadata and the
    vector similarity as both score and relevance.
    """
    payload = hit.payload or {}
    return {
        "qa_id": payload.get("mongo_id"),
        "question": payload.get("original_question"),
        "answer": payload.get("text"),
        "metadata": payload.get("custom_metadata", {}),
        # Qdrant vector similarity (higher is better)
        "score": hit.score,
        "relevance": hit.score,
    }


def _fetch_qa_candidates(
    qdrant_client: QdrantClient,
    collection_name: str,
    query_vector: List[float],
    candidate_limit: int,
    qdrant_filter: Optional[Filter],
) -> List[Dict[str, Any]]:
    """Vector search of the candidate pool in the tenant's Q&A collection: one candidate per hit, in Qdrant score
    order.
    """
    # Search in Qdrant (candidate pool)
    qdrant_hits = qdrant_client.search(
        collection_name=collection_name,
        query_vector=query_vector,
        limit=candidate_limit,
        query_filter=qdrant_filter,
        with_payload=True,
    )

    # Format candidates (vector similarity from Qdrant).
    candidates: List[Dict[str, Any]] = []
    for hit in qdrant_hits or []:
        candidates.append(_qa_hit_to_candidate(hit))
    return candidates


def _compose_rerank_documents(candidates: List[Dict[str, Any]]) -> List[str]:
    """One "Question: ... Answer: ..." text per candidate, same order: the documents Cohere reranks."""
    # Build rerank documents. Include both Q and A for better disambiguation.
    rerank_documents: List[str] = []
    for candidate in candidates:
        question_text = (candidate.get("question") or "").strip()
        answer_text = (candidate.get("answer") or "").strip()
        rerank_documents.append(f"Question: {question_text}\nAnswer: {answer_text}".strip())
    return rerank_documents


def _apply_rerank_scores(rerank_response: Any, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of the candidates Cohere ranked, in rerank order, with the rerank relevance as both score and
    relevance; results without index / relevance_score or with an out-of-range index are skipped (may return []).
    """
    # rerank_response.results items: { index, relevance_score }
    reranked_candidates: List[Dict[str, Any]] = []
    for rerank_result in getattr(rerank_response, "results", []) or []:
        candidate_index = getattr(rerank_result, "index", None)
        relevance_score = getattr(rerank_result, "relevance_score", None)
        if candidate_index is None or relevance_score is None:
            continue
        if not (0 <= int(candidate_index) < len(candidates)):
            continue
        reranked_candidate = dict(candidates[int(candidate_index)])
        # Prefer rerank as the single score signal.
        # We intentionally DO NOT keep the Qdrant score in the output payload.
        reranked_candidate["score"] = float(relevance_score)
        reranked_candidate["relevance"] = float(relevance_score)
        reranked_candidates.append(reranked_candidate)
    return reranked_candidates


def _rerank_qa_candidates(query_text: str, candidates: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    """Best-effort Cohere rerank of the candidates; the first top_k in vector order when COHERE_API_KEY is missing,
    the rerank returns nothing usable, or anything fails.
    """
    # Cohere rerank (best-effort; fallback to vector order on any failure)
    try:
        import cohere  # type: ignore

        cohere_api_key = os.getenv("COHERE_API_KEY")
        if not cohere_api_key:
            return candidates[:top_k]

        rerank_model_name = os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-pro")

        rerank_documents = _compose_rerank_documents(candidates)

        cohere_client = cohere.ClientV2(api_key=cohere_api_key)
        rerank_response = cohere_client.rerank(
            model=rerank_model_name,
            query=query_text,
            documents=rerank_documents,
            top_n=min(top_k, len(rerank_documents)),
        )

        reranked_candidates = _apply_rerank_scores(rerank_response, candidates)

        return reranked_candidates if reranked_candidates else candidates[:top_k]
    except Exception:
        return candidates[:top_k]


def find_relevant_qa_pairs(
    tenant_name: str,
    query_text: str,
    top_k: int = 5,
    required_metadata: Optional[Dict[str, Any]] = None,
    query_image: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search in Q&A documents using Cohere embeddings and vector similarity.

    Args:
        tenant_name: Name of the tenant
        query_text: The search query (text)
        top_k: Number of top results to return
        required_metadata: Optional filter for metadata
        query_image: Optional base64 data URI of an image to search by.
                     When provided, the image is embedded and used for vector search
                     instead of the text query (multimodal similarity matching).

    Returns:
        List of relevant Q&A with scores
    """
    collection_name = _qa_collection_name(tenant_name)
    qdrant_client = ensure_qdrant_client()

    # Check if collection exists
    if not has_collection(qdrant_client, collection_name):
        logger.warning(f"Collection {collection_name} does not exist")
        return []

    # Create embedding for query using Cohere
    query_vector = _embed_search_query(query_text, query_image)
    is_image_query = bool(query_image)

    qdrant_filter = _build_qa_filter(required_metadata)

    # Fetch more candidates than top_k, then Cohere-rerank them.
    # Cohere rerank performs best with a reasonable candidate set (<= 200-500).
    candidate_limit = max(top_k * 10, 50)
    candidate_limit = min(candidate_limit, 200)

    candidates = _fetch_qa_candidates(qdrant_client, collection_name, query_vector, candidate_limit, qdrant_filter)

    if not candidates:
        return []

    # For image queries, skip text-based Cohere rerank (it only works on text).
    # Vector similarity from Cohere embed-v4.0 multimodal tenant is already meaningful.
    if is_image_query:
        return candidates[:top_k]

    return _rerank_qa_candidates(query_text, candidates, top_k)


def remove_qa_pair_from_index(qa_pair_id: str, tenant_name: str) -> Dict[str, Any]:
    """
    Delete a Q&A from Qdrant collection.

    Args:
        qa_pair_id: ID of the QADocument
        tenant_name: Name of the tenant

    Returns:
        Dict with status
    """
    collection_name = _qa_collection_name(tenant_name)
    qdrant_client = ensure_qdrant_client()

    try:
        # Convert MongoDB ID to Qdrant point ID
        point_id = _derive_qa_point_id(qa_pair_id)

        qdrant_client.delete(
            collection_name=collection_name,
            points_selector=[point_id],
        )
        logger.info(f"Deleted Q&A {qa_pair_id} from Qdrant collection {collection_name}")
        return {"status": "success", "qa_id": qa_pair_id, "collection_name": collection_name}
    except Exception as error:
        logger.error(f"Error deleting Q&A from Qdrant: {error}")
        return {"status": "error", "error": str(error)}
