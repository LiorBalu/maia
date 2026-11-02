"""Knowledge documents of a tenant and their embedded parts (chunks indexed in Qdrant)."""

from typing import Optional
from mongoengine import (
    Document,
    StringField,
    IntField,
    ListField,
    BooleanField,
    DateTimeField,
    FloatField,
)
from datetime import datetime
from pydantic import BaseModel, Field


class EmbeddedPart(Document):
    """
    Independent document for storing document parts - avoids 16MB MongoDB limit.
    
    Cohere Embedding:
    - Model: Cohere embed-v4.0 (1024 dimensions)
    - Each part is embedded separately for semantic search
    """

    document_id = StringField(required=True, index=True)  # Reference to parent document
    tenant_name = StringField(required=True, index=True)
    part_number = IntField(required=True)
    content = StringField(required=True)
    vector_id = StringField()
    vector_for_backup = ListField(FloatField())
    created_at = DateTimeField()
    updated_at = DateTimeField()

    meta = {
        "collection": "knowledge_chunks",
        "indexes": [
            {"fields": ["document_id", "part_number"]},
            {"fields": ["tenant_name"]},
        ],
    }

    def save(self, *args, **kwargs):
        """Stamp created_at / updated_at."""
        if not self.created_at:
            self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)


class EmbeddedDocument(Document):
    """A knowledge document of a tenant (uploaded file or scraped site): title, source URL, categories, private flag
    and indexing state; its chunks are EmbeddedPart documents.
    """
    tenant_name = StringField(required=True)
    title = StringField(required=True)
    content = StringField()
    url = StringField()
    is_indexed = BooleanField(default=False)
    # When True, this document is an automatically generated RAG memory for a specific WhatsApp group.
    # These docs should NOT be included in the default "document_ids=None => all docs" search.
    is_private = BooleanField(default=False)
    categories = ListField(StringField())
    total_parts = IntField(default=0)  # Number of parts
    created_at = DateTimeField()
    updated_at = DateTimeField()
    meta = {"collection": "knowledge_documents"}

    def save(self, *args, **kwargs):
        """Stamp created_at / updated_at."""
        if not self.created_at:
            self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)


class EmbeddedPartResponse(BaseModel):
    """A document part as returned by the API."""
    id: Optional[str] = Field(None)
    part_number: int
    content: str
    vector_id: Optional[str] = Field(None)
    vector_for_backup: Optional[list[float]] = Field(None)
