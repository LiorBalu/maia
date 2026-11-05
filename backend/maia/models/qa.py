"""QADocument: a question/answer pair of the tenant's Q&A bank, with its Qdrant index state."""

from typing import Optional
from mongoengine import (
    Document,
    StringField,
    BooleanField,
    DateTimeField,
    DictField,
    ListField,
    FloatField,
)
from datetime import datetime
from pydantic import BaseModel, Field


class QADocument(Document):
    """
    Question & Answer document for RAG system.
    
    Cohere Embedding Strategy:
    - EMBED: The question field only (semantic search target)
    - PAYLOAD: Answer stored in Qdrant for retrieval/reranking
    - Model: Cohere embed-v4.0 (1024 dimensions)
    """

    tenant_name = StringField(required=True, index=True)
    question = StringField(required=True)
    answer = StringField(required=True)

    # Vector storage
    vector_id = StringField()  # ID in Qdrant
    vector_for_backup = ListField(FloatField())  # Backup vector storage

    # Indexing status
    is_indexed = BooleanField(default=False)

    # Optional free-form metadata (filterable in the Q&A search)
    metadata = DictField(default=dict)  # Can store: tags, priority, etc.

    # Timestamps
    created_at = DateTimeField()
    updated_at = DateTimeField()

    meta = {
        "collection": "qa_pairs",
        "indexes": [
            {"fields": ["tenant_name"]},
            {"fields": ["tenant_name", "is_indexed"]},
            {"fields": ["created_at"]},
        ],
    }

    def save(self, *args, **kwargs):
        """Stamp created_at / updated_at."""
        if not self.created_at:
            self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)


# Pydantic models for API responses
class QADocumentResponse(BaseModel):
    """Response model for Q&A documents"""

    id: str
    tenant_name: str
    question: str
    answer: str
    is_indexed: bool
    metadata: dict = Field(default_factory=dict)
    vector_id: Optional[str] = Field(None)
    created_at: str
    updated_at: str


class QADocumentCreate(BaseModel):
    """Request model for creating Q&A"""

    question: str
    answer: str
    tenant_name: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class QADocumentUpdate(BaseModel):
    """Request model for updating Q&A"""

    question: Optional[str] = None
    answer: Optional[str] = None
    metadata: Optional[dict] = None
