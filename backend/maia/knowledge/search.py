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
    docs_collection_name,
    get_qdrant_client,
)

logger = logging.getLogger(__name__)


def search_in_documents(
    tenant_name: str,
    query: str,
    document_ids: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    top_k: int = 5,
) -> List[dict]:
    """
    Search across multiple documents in a tenant using Cohere embeddings.

    Args:
        tenant_name: Name of the tenant
        query: The search query
        document_ids: List of specific document IDs to search in. If None, searches all documents in the tenant
        categories: Optional list of categories to filter documents by
        top_k: Number of top results to return

    Returns:
        List of relevant document chunks with metadata
    """
    # Create Cohere embeddings for query
    embeddings = CohereEmbeddingsV4(input_type="search_query")
    query_vector = embeddings.embed_query(query)

    client = get_qdrant_client()

    # Fetch more candidates than top_k, then Cohere-rerank them (best-effort).
    # Keep a reasonable upper bound to avoid expensive rerank calls.
    candidate_k = max(top_k * 10, 50)
    candidate_k = min(candidate_k, 200)

    # Single query against the shared per-tenant collection, scoped by payload
    # filter. Selection semantics preserved:
    # - document_ids is None : all *public* docs in the tenant (exclude private)
    # - document_ids is []   : no docs -> empty result
    # - document_ids is [..] : exactly those docs (private flag ignored, as before)
    if document_ids is not None and len(document_ids) == 0:
        return []

    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    must = [FieldCondition(key="tenant_name", match=MatchValue(value=tenant_name))]
    if document_ids is not None:
        must.append(FieldCondition(key="document_id", match=MatchAny(any=[str(d) for d in document_ids])))
    else:
        must.append(FieldCondition(key="is_private", match=MatchValue(value=False)))
        cats = [c.strip() for c in (categories or []) if c and str(c).strip()]
        if cats:
            must.append(FieldCondition(key="categories", match=MatchAny(any=cats)))

    collection_name = docs_collection_name(tenant_name)
    all_results = []
    try:
        results = client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            query_filter=Filter(must=must),
            limit=candidate_k,
            with_payload=True,
        )
        for hit in results:
            payload = hit.payload or {}
            all_results.append(
                {
                    "content": payload.get("page_content", ""),
                    "metadata": {
                        "source_type": "document",
                        "document_id": str(payload.get("document_id", "")),
                        "document_title": "",  # filled via batch lookup below
                        "part_number": payload.get("part_number"),
                        "tenant_name": tenant_name,
                        "vector_score": hit.score,
                    },
                    "score": hit.score,
                }
            )
    except Exception as e:
        logger.error(f"Warning: Could not search in {collection_name}: {e}")
        return []

    if not all_results:
        return []

    # Resolve document titles in one batched Mongo lookup (a handful of distinct
    # ids), instead of carrying the title in every Qdrant payload (stale-prone).
    doc_ids = {r["metadata"]["document_id"] for r in all_results if r["metadata"]["document_id"]}
    titles = {str(d.id): d.title for d in EmbeddedDocument.objects(id__in=list(doc_ids)).only("id", "title")}
    for r in all_results:
        r["metadata"]["document_title"] = titles.get(r["metadata"]["document_id"], "")

    # Already score-ordered by Qdrant; trim to the global candidate pool.
    candidates = all_results[:candidate_k]

    # Cohere rerank (best-effort; fallback to vector order on any failure)
    try:
        import os

        import cohere  # type: ignore

        cohere_api_key = os.getenv("COHERE_API_KEY")
        if not cohere_api_key:
            return candidates[:top_k]

        rerank_model = os.getenv("COHERE_RERANK_MODEL", "rerank-v4.0-pro")

        # Build rerank documents: title + chunk content
        rerank_docs: List[str] = []
        for c in candidates:
            md = c.get("metadata", {}) or {}
            title = (md.get("document_title") or "").strip()
            content = (c.get("content") or "").strip()
            if title:
                rerank_docs.append(f"Title: {title}\n\n{content}".strip())
            else:
                rerank_docs.append(content)

        co = cohere.ClientV2(api_key=cohere_api_key)
        rr = co.rerank(model=rerank_model, query=query, documents=rerank_docs, top_n=min(top_k, len(rerank_docs)))

        reranked: List[dict] = []
        for item in getattr(rr, "results", []) or []:
            idx = getattr(item, "index", None)
            score = getattr(item, "relevance_score", None)
            if idx is None or score is None:
                continue
            if not (0 <= int(idx) < len(candidates)):
                continue
            c = dict(candidates[int(idx)])
            # Keep Qdrant score in metadata.vector_score; expose rerank score as "score"
            c["score"] = float(score)
            reranked.append(c)

        return reranked if reranked else candidates[:top_k]
    except Exception:
        return candidates[:top_k]


class MultiDocumentRetriever(BaseRetriever):
    """
    Simple retriever that searches across multiple Qdrant collections using Cohere embeddings.
    Compatible with LangChain's Pydantic-based BaseRetriever.
    """

    # Pydantic model fields
    tenant_name: str = ""
    document_ids: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    top_k: int = 5

    class Config:
        """Pydantic configuration"""

        arbitrary_types_allowed = True

    def __init__(
        self,
        tenant_name: str,
        document_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        top_k: int = 5,
    ):
        """Pydantic init with the tenant, optional document ids / categories and top_k."""
        super().__init__(tenant_name=tenant_name, document_ids=document_ids, categories=categories, top_k=top_k)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """
        Retrieve documents using Cohere embeddings.
        """
        results = search_in_documents(
            tenant_name=self.tenant_name,
            query=query,
            document_ids=self.document_ids,
            categories=self.categories,
            top_k=self.top_k,
        )

        # Convert to LangChain documents and store final score in metadata
        docs = []
        for result in results:
            metadata = result["metadata"]
            # Store the final score (rerank score if available, else vector score)
            # so CombinedRetriever can apply score_threshold filtering
            metadata["final_score"] = result.get("score", metadata.get("vector_score", 0))
            docs.append(LangchainDocument(page_content=result["content"], metadata=metadata))

        return docs


class QARetriever(BaseRetriever):
    """
    Retriever for Q&A documents using Cohere embeddings.
    Supports image-based search when current_image is set.
    """

    tenant_name: str = ""
    top_k: int = 5
    categories: Optional[List[str]] = None
    metadata_filter: Optional[Dict[str, Any]] = None
    current_image: Optional[str] = None  # Base64 data URI for image search

    class Config:
        """Pydantic configuration."""
        arbitrary_types_allowed = True

    def __init__(
        self,
        tenant_name: str,
        top_k: int = 5,
        categories: Optional[List[str]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ):
        """Pydantic init with the tenant, top_k, categories and the metadata filter."""
        super().__init__(tenant_name=tenant_name, top_k=top_k, categories=categories, metadata_filter=metadata_filter)

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> List[LangchainDocument]:
        """Retrieve relevant Q&A documents using Cohere."""
        from maia.knowledge.qa import search_in_qa

        results = search_in_qa(
            tenant_name=self.tenant_name,
            query=query,
            top_k=self.top_k,
            metadata_filter=self.metadata_filter,
            categories=self.categories,
            query_image=self.current_image,
        )

        docs = []
        for result in results:
            # Format Q&A as document for RAG context
            content = f"Question: {result['question']}\nAnswer: {result['answer']}"
            custom_meta = result.get("metadata", {}) or {}
            final_score = result.get("score", result.get("relevance", 0))
            docs.append(
                LangchainDocument(
                    page_content=content,
                    metadata={
                        **custom_meta,
                        "qa_id": result.get("qa_id"),
                        "question": result.get("question"),
                        "qa_answer": result.get("answer"),
                        # NOTE: `vector_score` is what the UI shows as sources[].score.
                        # We prefer `result["vector_score"]` (rerank score when available),
                        # and fall back to `result["score"]`.
                        "vector_score": result.get("vector_score", result.get("score")),
                        "final_score": final_score,
                        "source_type": "qa",
                    },
                )
            )

        return docs


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

        all_docs = []

        for retriever in self.retrievers:
            try:
                # Propagate image to QARetriever for multimodal search
                if self.current_image and isinstance(retriever, QARetriever):
                    retriever.current_image = self.current_image
                docs = retriever.invoke(query, config={"callbacks": run_manager.get_child()})
                all_docs.extend(docs)
            except Exception as e:
                logger.error(f"Warning: Skipping one retriever: {e}")
                continue

        # Deduplicate by content
        seen_content = set()
        unique_docs = []
        for doc in all_docs:
            if not doc.page_content or not doc.page_content.strip():
                continue

            content_id = doc.page_content[:100].strip()
            if content_id and content_id not in seen_content:
                seen_content.add(content_id)
                unique_docs.append(doc)

        doc_docs: List[LangchainDocument] = []
        qa_docs: List[LangchainDocument] = []

        for doc in unique_docs:
            meta = getattr(doc, "metadata", None) or {}
            source_type = meta.get("source_type")
            if source_type == "qa":
                qa_docs.append(doc)
            elif source_type == "document":
                doc_docs.append(doc)

        # Apply score_threshold filtering if configured (> 0)
        if self.score_threshold and self.score_threshold > 0:
            before_doc = len(doc_docs)
            before_qa = len(qa_docs)
            doc_docs = [
                d
                for d in doc_docs
                if (getattr(d, "metadata", None) or {}).get("final_score", 1) >= self.score_threshold
            ]
            qa_docs = [
                d for d in qa_docs if (getattr(d, "metadata", None) or {}).get("final_score", 1) >= self.score_threshold
            ]
            filtered_out = (before_doc - len(doc_docs)) + (before_qa - len(qa_docs))
            if filtered_out > 0:
                logger.info(f"Score threshold {self.score_threshold}: filtered out {filtered_out} results "
                    f"(docs: {before_doc}→{len(doc_docs)}, qa: {before_qa}→{len(qa_docs)})")

        # Balanced selection (no cross-filling; strict per-source caps)
        selected = doc_docs[: max(0, int(self.documents_top_k or 0))] + qa_docs[: max(0, int(self.qa_top_k or 0))]

        # Without source_type tags (a single retriever) keep the top_k of the merged list
        if not selected:
            selected = unique_docs[: self.top_k]

        return selected
