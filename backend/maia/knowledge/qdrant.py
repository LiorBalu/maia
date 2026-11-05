"""
Qdrant Helper Functions for Cohere RAG Engine

Provides:
- Centralized Qdrant client configuration
- Collection creation with int8 quantization optimized for Cohere embed-v4.0
- Collection management utilities
"""

import logging
import uuid
from os import getenv
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    VectorParams,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
)

logger = logging.getLogger(__name__)

# Qdrant configuration (the local container: host + port)
QDRANT_HOST = getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(getenv("QDRANT_PORT", "6333"))

# Cohere embed-v4.0 configuration
COHERE_EMBEDDING_DIMENSIONS = 1024

# Module-level client (lazy initialization)
_shared_qdrant_client = None


def ensure_qdrant_client() -> QdrantClient:
    """
    Get configured Qdrant client (singleton pattern), connected to QDRANT_HOST:QDRANT_PORT.
    """
    global _shared_qdrant_client

    if _shared_qdrant_client is None:
        try:
            _shared_qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
            logger.info(f"Qdrant client initialized (Local): {QDRANT_HOST}:{QDRANT_PORT}")
        except Exception as error:
            logger.error(f"Qdrant client initialization failed: {error}")
            raise

    return _shared_qdrant_client


def _create_cohere_vector_collection(
    qdrant_client: QdrantClient, collection_name: str, should_store_vectors_on_disk: bool
) -> None:
    """Create a collection for Cohere embed-v4.0 vectors: 1024 dimensions, cosine distance, int8 scalar quantization."""
    qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=COHERE_EMBEDDING_DIMENSIONS, distance=Distance.COSINE, on_disk=should_store_vectors_on_disk),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,
                quantile=0.99,
                always_ram=True,  # Keep quantized vectors in RAM for speed
            )
        ),
    )


def create_qa_collection(qdrant_client: QdrantClient, collection_name: str, should_store_vectors_on_disk: bool = True) -> None:
    """
    Create a Qdrant collection optimized for Cohere embed-v4.0.

    Features:
    - 1024 dimensions (Cohere embed-v4.0)
    - Cosine distance
    - int8 scalar quantization for memory/speed optimization

    Args:
        qdrant_client: Qdrant client instance
        collection_name: Name for the collection
        should_store_vectors_on_disk: Whether to store vectors on disk (default: True)
    """
    _create_cohere_vector_collection(qdrant_client, collection_name, should_store_vectors_on_disk)
    logger.info(f"Created Qdrant collection '{collection_name}' with int8 quantization (1024 dims)")


# ============================================================================
# Shared per-tenant documents collection ({tenant_name}_docs)
#
# All parts of every document in a tenant live in a single collection; selection
# (specific docs, private/public) is done via payload filters at query time.
# ============================================================================

DOCUMENTS_COLLECTION_SUFFIX = "_docs"


def derive_tenant_documents_collection_name(tenant_name: str) -> str:
    """Canonical name of the shared documents collection for a tenant."""
    return f"{tenant_name}{DOCUMENTS_COLLECTION_SUFFIX}"


def derive_document_part_point_id(knowledge_document_id: str, part_number: int) -> str:
    """
    Deterministic, globally-unique Qdrant point id for a document part.

    A stable UUID5 of ``knowledge_document_id:part_number``: unique across the documents that share the
    tenant's collection, and stable so that re-ingesting the same part overwrites it instead
    of duplicating it.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{knowledge_document_id}:{part_number}"))


def _create_documents_payload_indexes(qdrant_client: QdrantClient, collection_name: str) -> None:
    """Create the payload indexes the query-time filters rely on: tenant_name, document_id, is_private."""
    for payload_field_name, payload_field_schema in (
        ("tenant_name", PayloadSchemaType.KEYWORD),
        ("document_id", PayloadSchemaType.KEYWORD),
        ("is_private", PayloadSchemaType.BOOL),
    ):
        qdrant_client.create_payload_index(
            collection_name=collection_name, field_name=payload_field_name, field_schema=payload_field_schema
        )


def create_tenant_documents_collection(qdrant_client: QdrantClient, collection_name: str, should_store_vectors_on_disk: bool = True) -> None:
    """
    Create the shared per-tenant documents collection optimized for Cohere
    embed-v4.0, with the payload indexes the query-time filters rely on:

    - ``tenant_name``  (KEYWORD) — tenant isolation
    - ``document_id`` (KEYWORD) — scope a search to specific documents / delete
    - ``is_private``  (BOOL)    — exclude group-generated docs from default search
    """
    _create_cohere_vector_collection(qdrant_client, collection_name, should_store_vectors_on_disk)
    _create_documents_payload_indexes(qdrant_client, collection_name)
    logger.info(f"Created shared documents collection '{collection_name}' (1024 dims, int8, payload-indexed)")


def ensure_tenant_documents_collection(qdrant_client: QdrantClient, tenant_name: str) -> str:
    """
    Return the shared documents collection name for the tenant, creating it
    (with payload indexes) if it does not exist yet. Never drops an existing
    collection — safe to call on every document ingest.
    """
    collection_name = derive_tenant_documents_collection_name(tenant_name)
    if not has_collection(qdrant_client, collection_name):
        try:
            create_tenant_documents_collection(qdrant_client, collection_name)
        except Exception as create_error:
            # Two documents of a new tenant are ingested in parallel: the other worker
            # created the collection between our check and our create (Qdrant answers 409).
            if not has_collection(qdrant_client, collection_name):
                raise
            logger.error(
                f"ℹCollection '{collection_name}' was created concurrently, continuing ({type(create_error).__name__})"
            )
    return collection_name


def has_collection(qdrant_client: QdrantClient, collection_name: str) -> bool:
    """
    Check if a collection exists in Qdrant.

    Args:
        qdrant_client: Qdrant client instance
        collection_name: Collection name to check

    Returns:
        True if collection exists, False otherwise
    """
    try:
        qdrant_client.get_collection(collection_name)
        return True
    except Exception:
        return False
