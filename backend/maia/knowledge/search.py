"""LangChain retrievers over the tenant's knowledge: document parts (Qdrant), the Q&A bank, and the combination the RAG chain queries."""


import logging
from typing import Any, Dict, List, Optional

# LangChain 1.0 imports - functions moved to langchain_classic
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document as LangchainDocument
from langchain_core.retrievers import BaseRetriever

from maia.knowledge.embeddings import CohereEmbeddingsV4
from maia.models.knowledge_documents import EmbeddedDocument
from maia.knowledge.qdrant import (
    derive_tenant_documents_collection_name,
    ensure_qdrant_client,
)

logger = logging.getLogger(__name__)


def _build_chunk_search_filter(tenant_name: str, selected_document_ids: Optional[List[str]]):
    """Qdrant payload filter (a qdrant_client.models.Filter) of the documents search: the tenant, then exactly the
    selected document ids (private flag ignored) or, when no ids were selected, the public documents.
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    filter_conditions = [FieldCondition(key="tenant_name", match=MatchValue(value=tenant_name))]
    if selected_document_ids is not None:
        filter_conditions.append(
            FieldCondition(
                key="document_id", match=MatchAny(any=[str(document_id) for document_id in selected_document_ids])
            )
        )
    else:
        filter_conditions.append(FieldCondition(key="is_private", match=MatchValue(value=False)))
    return Filter(must=filter_conditions)


def _chunk_result_from_hit(hit, tenant_name: str) -> dict:
    """One chunk result from a Qdrant hit of the documents collection: content, metadata (document_title is filled
    later by _attach_document_titles) and the vector score.
    """
    payload = hit.payload or {}
    return {
        "content": payload.get("page_content", ""),
        "metadata": {
            "source_type": "document",
            "document_id": str(payload.get("document_id", "")),
            "document_title": "",  # filled via batch lookup (_attach_document_titles)
            "part_number": payload.get("part_number"),
            "tenant_name": tenant_name,
            "vector_score": hit.score,
        },
        "score": hit.score,
    }


def _fetch_candidate_chunks(
    qdrant_client, tenant_name: str, query_vector: List[float], search_filter, candidate_limit: int
) -> List[dict]:
    """The single filtered vector search on the tenant's documents collection: one chunk result per hit, in Qdrant
    score order; [] (logged) when the search fails.
    """
    collection_name = derive_tenant_documents_collection_name(tenant_name)
    chunk_results = []
    try:
        qdrant_hits = qdrant_client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            query_filter=search_filter,
            limit=candidate_limit,
            with_payload=True,
        )
        for hit in qdrant_hits:
            chunk_results.append(_chunk_result_from_hit(hit, tenant_name))
    except Exception as error:
        logger.error(f"Warning: Could not search in {collection_name}: {error}")
        return []
    return chunk_results


def _attach_document_titles(chunk_results: List[dict]) -> None:
    """Fill metadata["document_title"] of every chunk result in place ("" when the document is not found)."""
    # Resolve document titles in one batched Mongo lookup (a handful of distinct
    # ids), instead of carrying the title in every Qdrant payload (stale-prone).
    distinct_document_ids = {
        chunk_result["metadata"]["document_id"]
        for chunk_result in chunk_results
        if chunk_result["metadata"]["document_id"]
    }
    title_by_document_id = {
        str(document.id): document.title
        for document in EmbeddedDocument.objects(id__in=list(distinct_document_ids)).only("id", "title")
    }
    for chunk_result in chunk_results:
        chunk_result["metadata"]["document_title"] = title_by_document_id.get(
            chunk_result["metadata"]["document_id"], ""
        )


def _compose_rerank_texts(candidates: List[dict]) -> List[str]:
    """One rerank text per candidate, same order: "Title: <title>" and the chunk text when the title is known, else
    the chunk text alone.
    """
    # Build rerank documents: title + chunk content
    rerank_texts: List[str] = []
    for candidate in candidates:
        chunk_metadata = candidate.get("metadata", {}) or {}
        document_title = (chunk_metadata.get("document_title") or "").strip()
        chunk_text = (candidate.get("content") or "").strip()
        if document_title:
            rerank_texts.append(f"Title: {document_title}\n\n{chunk_text}".strip())
        else:
            rerank_texts.append(chunk_text)
    return rerank_texts


def _apply_rerank_scores(rerank_response, candidates: List[dict]) -> List[dict]:
    """Copies of the candidates Cohere ranked, in rerank order, with the rerank relevance as "score"; results without
    index / relevance_score or with an out-of-range index are skipped (may return []).
    """
    reranked_candidates: List[dict] = []
    for rerank_result in getattr(rerank_response, "results", []) or []:
        candidate_index = getattr(rerank_result, "index", None)
        relevance_score = getattr(rerank_result, "relevance_score", None)
        if candidate_index is None or relevance_score is None:
            continue
        if not (0 <= int(candidate_index) < len(candidates)):
            continue
        reranked_candidate = dict(candidates[int(candidate_index)])
        # Keep Qdrant score in metadata.vector_score; expose rerank score as "score"
        reranked_candidate["score"] = float(relevance_score)
        reranked_candidates.append(reranked_candidate)
    return reranked_candidates


def _rerank_document_chunks(query_text: str, candidates: List[dict], top_k: int) -> List[dict]:
    """Best-effort Cohere rerank of the candidate chunks; the first top_k in vector order when COHERE_API_KEY is
    missing, the rerank returns nothing usable, or anything fails.
    """
    # Cohere rerank (best-effort; fallback to vector order on any failure)
    try:
        import os

        import cohere  # type: ignore

        cohere_api_key = os.getenv("COHERE_API_KEY")
        if not cohere_api_key:
            return candidates[:top_k]

        rerank_model_name = os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-pro")

        rerank_texts = _compose_rerank_texts(candidates)

        cohere_client = cohere.ClientV2(api_key=cohere_api_key)
        rerank_response = cohere_client.rerank(
            model=rerank_model_name, query=query_text, documents=rerank_texts, top_n=min(top_k, len(rerank_texts))
        )

        reranked_candidates = _apply_rerank_scores(rerank_response, candidates)

        return reranked_candidates if reranked_candidates else candidates[:top_k]
    except Exception:
        return candidates[:top_k]


def find_relevant_document_parts(
    tenant_name: str,
    query_text: str,
    selected_document_ids: Optional[List[str]] = None,
    top_k: int = 5,
) -> List[dict]:
    """
    Search across multiple documents in a tenant using Cohere embeddings.

    Args:
        tenant_name: Name of the tenant
        query_text: The search query
        selected_document_ids: List of specific document IDs to search in. If None, searches all documents in the tenant
        top_k: Number of top results to return

    Returns:
        List of relevant document chunks with metadata
    """
    # Create Cohere embeddings for query
    query_embedder = CohereEmbeddingsV4(input_type="search_query")
    query_vector = query_embedder.embed_query(query_text)

    qdrant_client = ensure_qdrant_client()

    # Fetch more candidates than top_k, then Cohere-rerank them (best-effort).
    # Keep a reasonable upper bound to avoid expensive rerank calls.
    candidate_limit = max(top_k * 10, 50)
    candidate_limit = min(candidate_limit, 200)

    # Single query against the shared per-tenant collection, scoped by payload
    # filter. Selection semantics preserved:
    # - selected_document_ids is None : all *public* docs in the tenant (exclude private)
    # - selected_document_ids is []   : no docs -> empty result
    # - selected_document_ids is [..] : exactly those docs (private flag ignored, as before)
    if selected_document_ids is not None and len(selected_document_ids) == 0:
        return []

    search_filter = _build_chunk_search_filter(tenant_name, selected_document_ids)
    chunk_results = _fetch_candidate_chunks(qdrant_client, tenant_name, query_vector, search_filter, candidate_limit)

    if not chunk_results:
        return []

    _attach_document_titles(chunk_results)

    # Already score-ordered by Qdrant; trim to the global candidate pool.
    candidates = chunk_results[:candidate_limit]

    return _rerank_document_chunks(query_text, candidates, top_k)


def _chunk_result_to_document(chunk_result: dict) -> LangchainDocument:
    """LangChain document of one chunk result; sets metadata["final_score"] in place (rerank score, else vector
    score) for the score_threshold of CombinedRetriever.
    """
    chunk_metadata = chunk_result["metadata"]
    # Store the final score (rerank score if available, else vector score)
    # so CombinedRetriever can apply score_threshold filtering
    chunk_metadata["final_score"] = chunk_result.get("score", chunk_metadata.get("vector_score", 0))
    return LangchainDocument(page_content=chunk_result["content"], metadata=chunk_metadata)


class MultiDocumentRetriever(BaseRetriever):
    """
    Simple retriever that searches across multiple Qdrant collections using Cohere embeddings.
    Compatible with LangChain's Pydantic-based BaseRetriever.
    """

    # Pydantic model fields
    tenant_name: str = ""
    document_ids: Optional[List[str]] = None
    top_k: int = 5

    class Config:
        """Pydantic configuration"""

        arbitrary_types_allowed = True

    def __init__(
        self,
        tenant_name: str,
        document_ids: Optional[List[str]] = None,
        top_k: int = 5,
    ):
        """Pydantic init with the tenant, optional document ids and top_k."""
        super().__init__(tenant_name=tenant_name, document_ids=document_ids, top_k=top_k)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """
        Retrieve documents using Cohere embeddings.
        """
        chunk_results = find_relevant_document_parts(
            tenant_name=self.tenant_name,
            query_text=query,
            selected_document_ids=self.document_ids,
            top_k=self.top_k,
        )

        # Convert to LangChain documents and store final score in metadata
        langchain_documents = [_chunk_result_to_document(chunk_result) for chunk_result in chunk_results]

        return langchain_documents


def _qa_match_to_document(qa_match: Dict[str, Any]) -> LangchainDocument:
    """LangChain document of one Q&A match: the question and answer as content; the pair's custom metadata plus
    qa_id, question, qa_answer, vector_score, final_score and source_type "qa" as metadata.
    """
    # Format Q&A as document for RAG context
    qa_text = f"Question: {qa_match['question']}\nAnswer: {qa_match['answer']}"
    pair_metadata = qa_match.get("metadata", {}) or {}
    final_score = qa_match.get("score", qa_match.get("relevance", 0))
    return LangchainDocument(
        page_content=qa_text,
        metadata={
            **pair_metadata,
            "qa_id": qa_match.get("qa_id"),
            "question": qa_match.get("question"),
            "qa_answer": qa_match.get("answer"),
            # NOTE: `vector_score` is what the UI shows as sources[].score.
            # We prefer `qa_match["vector_score"]` (rerank score when available),
            # and fall back to `qa_match["score"]`.
            "vector_score": qa_match.get("vector_score", qa_match.get("score")),
            "final_score": final_score,
            "source_type": "qa",
        },
    )


class QARetriever(BaseRetriever):
    """
    Retriever for Q&A documents using Cohere embeddings.
    Supports image-based search when current_image is set.
    """

    tenant_name: str = ""
    top_k: int = 5
    metadata_filter: Optional[Dict[str, Any]] = None
    current_image: Optional[str] = None  # Base64 data URI for image search

    class Config:
        """Pydantic configuration."""
        arbitrary_types_allowed = True

    def __init__(
        self,
        tenant_name: str,
        top_k: int = 5,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ):
        """Pydantic init with the tenant, top_k and the metadata filter."""
        super().__init__(tenant_name=tenant_name, top_k=top_k, metadata_filter=metadata_filter)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """Retrieve relevant Q&A documents using Cohere."""
        from maia.knowledge.qa import find_relevant_qa_pairs

        qa_matches = find_relevant_qa_pairs(
            tenant_name=self.tenant_name,
            query_text=query,
            top_k=self.top_k,
            required_metadata=self.metadata_filter,
            query_image=self.current_image,
        )

        langchain_documents = [_qa_match_to_document(qa_match) for qa_match in qa_matches]

        return langchain_documents


def _deduplicate_by_content(documents: List[LangchainDocument]) -> List[LangchainDocument]:
    """Drop the documents with blank content and keep the first document per key (the first 100 characters of its
    content, stripped).
    """
    # Deduplicate by content
    seen_content_keys = set()
    unique_documents = []
    for document in documents:
        if not document.page_content or not document.page_content.strip():
            continue

        content_key = document.page_content[:100].strip()
        if content_key and content_key not in seen_content_keys:
            seen_content_keys.add(content_key)
            unique_documents.append(document)
    return unique_documents


def _split_by_source_type(
    documents: List[LangchainDocument],
) -> tuple[List[LangchainDocument], List[LangchainDocument]]:
    """Partition by metadata["source_type"] into the "document" items and the "qa" items, order kept; items without
    a source_type go to neither list.
    """
    chunk_documents: List[LangchainDocument] = []
    qa_documents: List[LangchainDocument] = []

    for document in documents:
        document_metadata = getattr(document, "metadata", None) or {}
        source_type = document_metadata.get("source_type")
        if source_type == "qa":
            qa_documents.append(document)
        elif source_type == "document":
            chunk_documents.append(document)
    return chunk_documents, qa_documents


def _drop_below_score_threshold(
    chunk_documents: List[LangchainDocument], qa_documents: List[LangchainDocument], score_threshold: float
) -> tuple[List[LangchainDocument], List[LangchainDocument]]:
    """Keep the documents whose metadata["final_score"] (1 when missing) reaches score_threshold and log how many
    were dropped; both lists come back unchanged when the threshold is 0.
    """
    # Apply score_threshold filtering if configured (> 0)
    if score_threshold and score_threshold > 0:
        chunk_count_before = len(chunk_documents)
        qa_count_before = len(qa_documents)
        chunk_documents = [
            document
            for document in chunk_documents
            if (getattr(document, "metadata", None) or {}).get("final_score", 1) >= score_threshold
        ]
        qa_documents = [
            document
            for document in qa_documents
            if (getattr(document, "metadata", None) or {}).get("final_score", 1) >= score_threshold
        ]
        dropped_count = (chunk_count_before - len(chunk_documents)) + (qa_count_before - len(qa_documents))
        if dropped_count > 0:
            logger.info(f"Score threshold {score_threshold}: filtered out {dropped_count} results "
                f"(docs: {chunk_count_before}→{len(chunk_documents)}, qa: {qa_count_before}→{len(qa_documents)})")
    return chunk_documents, qa_documents


class CombinedRetriever(BaseRetriever):
    """
    Combines multiple retrievers and deduplicates results.
    Captures the reformulated query from history_aware_retriever.
    Supports image-based search by propagating current_image to QARetriever.
    """

    retrievers: List = []
    top_k: int = 10
    documents_top_k: int = 10
    qa_top_k: int = 5
    score_threshold: float = 0.0  # Minimum score to keep a result (0 = no filtering)
    last_reformulated_query: Optional[str] = None  # Capture reformulated question
    current_image: Optional[str] = None  # Base64 data URI for image search

    class Config:
        """Pydantic configuration."""
        arbitrary_types_allowed = True

    def __init__(
        self,
        retrievers: List,
        top_k: int = 10,
        documents_top_k: Optional[int] = None,
        qa_top_k: Optional[int] = None,
        score_threshold: float = 0.0,
    ):
        """Pydantic init with the retrievers and the per-source caps (qa_top_k defaults to 0, documents_top_k to
        top_k).
        """
        super().__init__(
            retrievers=retrievers,
            top_k=top_k,
            documents_top_k=documents_top_k if documents_top_k is not None else top_k,
            qa_top_k=qa_top_k if qa_top_k is not None else 0,
            score_threshold=score_threshold,
            last_reformulated_query=None,
        )

    def _collect_from_retrievers(
        self, query: str, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """Invoke every retriever with the query (the QARetriever also gets current_image) and concatenate their
        documents; a retriever that fails is logged and skipped.
        """
        retrieved_documents = []

        for retriever in self.retrievers:
            try:
                # Propagate image to QARetriever for multimodal search
                if self.current_image and isinstance(retriever, QARetriever):
                    retriever.current_image = self.current_image
                retriever_documents = retriever.invoke(query, config={"callbacks": run_manager.get_child()})
                retrieved_documents.extend(retriever_documents)
            except Exception as error:
                logger.error(f"Warning: Skipping one retriever: {error}")
                continue

        return retrieved_documents

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """
        Retrieve from all retrievers, deduplicate, then return a balanced set:
        - up to `documents_top_k` items where metadata.source_type == "document"
        - up to `qa_top_k` items where metadata.source_type == "qa"

        The 'query' parameter IS the reformulated question from history_aware_retriever.
        """
        # Capture the reformulated query passed by history_aware_retriever
        self.last_reformulated_query = query
        logger.info(f"🔄 Reformulated query captured: '{query[:100]}..."
            if len(query) > 100
            else f"🔄 Reformulated query captured: '{query}'")

        retrieved_documents = self._collect_from_retrievers(query, run_manager)

        deduplicated_documents = _deduplicate_by_content(retrieved_documents)

        chunk_documents, qa_documents = _split_by_source_type(deduplicated_documents)

        chunk_documents, qa_documents = _drop_below_score_threshold(chunk_documents, qa_documents, self.score_threshold)

        # Balanced selection (no cross-filling; strict per-source caps)
        selected_documents = (
            chunk_documents[: max(0, int(self.documents_top_k or 0))] + qa_documents[: max(0, int(self.qa_top_k or 0))]
        )

        # Without source_type tags (a single retriever) keep the top_k of the merged list
        if not selected_documents:
            selected_documents = deduplicated_documents[: self.top_k]

        return selected_documents
