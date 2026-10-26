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

# Qdrant configuration - supports both local and cloud
QDRANT_URL = getenv("QDRANT_URL")  # For cloud: https://xxx.qdrant.io
QDRANT_API_KEY = getenv("QDRANT_API_KEY")  # For cloud authentication
QDRANT_HOST = getenv("QDRANT_HOST", "localhost")  # For local: localhost
QDRANT_PORT = int(getenv("QDRANT_PORT", "6333"))  # For local: 6333

# Cohere embed-v4.0 configuration
COHERE_VECTOR_SIZE = 1024

# Module-level client (lazy initialization)
_qdrant_client = None


def get_qdrant_client() -> QdrantClient:
    """
    Get configured Qdrant client (singleton pattern).

    Supports both cloud (URL + API key) and local (host + port) configurations.
    """
    global _qdrant_client

    if _qdrant_client is None:
        try:
            if QDRANT_URL and QDRANT_API_KEY:
                _qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
                logger.info(f"Qdrant client initialized (Cloud): {QDRANT_URL}")
            else:
                _qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
                logger.info(f"Qdrant client initialized (Local): {QDRANT_HOST}:{QDRANT_PORT}")
        except Exception as e:
            logger.error(f"Qdrant client initialization failed: {e}")
            raise

    return _qdrant_client


def create_cohere_collection(client: QdrantClient, collection_name: str, on_disk: bool = True) -> None:
    """
    Create a Qdrant collection optimized for Cohere embed-v4.0.

    Features:
    - 1024 dimensions (Cohere embed-v4.0)
    - Cosine distance
    - int8 scalar quantization for memory/speed optimization

    Args:
        client: Qdrant client instance
        collection_name: Name for the collection
        on_disk: Whether to store vectors on disk (default: True)
    """
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=COHERE_VECTOR_SIZE, distance=Distance.COSINE, on_disk=on_disk),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,
                quantile=0.99,
                always_ram=True,  # Keep quantized vectors in RAM for speed
            )
        ),
    )
    client.create_payload_index(
        collection_name=collection_name, field_name="category", field_schema=PayloadSchemaType.KEYWORD
    )
    logger.info(f"Created Qdrant collection '{collection_name}' with int8 quantization (1024 dims)")


# ============================================================================
# Shared per-tenant documents collection ({tenant_name}_docs)
#
# All parts of every document in a tenant live in a single collection; selection
# (specific docs, categories, private/public) is done via payload filters at query time.
# ============================================================================

DOCS_COLLECTION_SUFFIX = "_docs"


def docs_collection_name(tenant_name: str) -> str:
    """Canonical name of the shared documents collection for a tenant."""
    return f"{tenant_name}{DOCS_COLLECTION_SUFFIX}"


def doc_point_id(document_id: str, part_number: int) -> str:
    """
    Deterministic, globally-unique Qdrant point id for a document part.

    A stable UUID5 of ``document_id:part_number``: unique across the documents that share the
    tenant's collection, and stable so that re-ingesting the same part overwrites it instead
    of duplicating it.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{part_number}"))


def create_documents_collection(client: QdrantClient, collection_name: str, on_disk: bool = True) -> None:
    """
    Create the shared per-tenant documents collection optimized for Cohere
    embed-v4.0, with the payload indexes the query-time filters rely on:

    - ``tenant_name``  (KEYWORD) — tenant isolation
    - ``document_id`` (KEYWORD) — scope a search to specific documents / delete
    - ``categories``  (KEYWORD) — category filtering (MatchAny over the list)
    - ``is_private``  (BOOL)    — exclude group-generated docs from default search
    """
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=COHERE_VECTOR_SIZE, distance=Distance.COSINE, on_disk=on_disk),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.99, always_ram=True)
        ),
    )
    for field_name, schema in (
        ("tenant_name", PayloadSchemaType.KEYWORD),
        ("document_id", PayloadSchemaType.KEYWORD),
        ("categories", PayloadSchemaType.KEYWORD),
        ("is_private", PayloadSchemaType.BOOL),
    ):
        client.create_payload_index(collection_name=collection_name, field_name=field_name, field_schema=schema)
    logger.info(f"Created shared documents collection '{collection_name}' (1024 dims, int8, payload-indexed)")


def ensure_documents_collection(client: QdrantClient, tenant_name: str) -> str:
    """
    Return the shared documents collection name for the tenant, creating it
    (with payload indexes) if it does not exist yet. Never drops an existing
    collection — safe to call on every document ingest.
    """
    name = docs_collection_name(tenant_name)
    if not collection_exists(client, name):
        try:
            create_documents_collection(client, name)
        except Exception as exc:
            # Two documents of a new tenant are ingested in parallel: the other worker
            # created the collection between our check and our create (Qdrant answers 409).
            if not collection_exists(client, name):
                raise
            logger.error(f"ℹCollection '{name}' was created concurrently, continuing ({type(exc).__name__})")
    return name


def collection_exists(client: QdrantClient, name: str) -> bool:
    """
    Check if a collection exists in Qdrant.

    Args:
        client: Qdrant client instance
        name: Collection name to check

    Returns:
        True if collection exists, False otherwise
    """
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False
