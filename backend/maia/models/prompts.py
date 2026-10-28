"""Prompt documents of a tenant (`default`, `platform_realtime`, named extras) and their edit history."""

import logging
import os
from datetime import datetime, timezone
from typing import List, Literal, Optional

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    FloatField,
    IntField,
    ListField,
    ReferenceField,
    StringField,
)
from odmantic import Field
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PromptPartModel(BaseModel):
    """A part of a structured prompt (category + content, optional embedding)."""
    category: str
    content: str
    always: bool = True
    embedding_vector: List[float] = []


class Prompt(BaseModel):
    """Prompt payload of the API (create / update / read)."""
    name: str
    tenant_name: str
    prompt: Optional[str] = Field(
        default=None,
        description="""**Prompt content for a text or structured type.
If it's a structured prompt, this field contains all the content from the different parts of the prompt.
If it's a text prompt, this field contains only the text content of the prompt.**""",
    )
    content: Optional[str] = None
    folders_for_tags: List[str] = []
    prompt_parts: List[PromptPartModel] = []
    prompt_type: Optional[Literal["text", "structured"]] = None
    modified_at: Optional[datetime] = datetime.now(timezone.utc)
    modified_by: Optional[str] = os.getenv("MAIA_DOCS_USER")
    label: Optional[str] = "ראשי"
    max_embedding_filter_parts: Optional[int] = 3

    class Config:
        """Example payload shown in the API docs."""
        json_schema_extra = {
            "example": {
                "name": "prompt_name",
                "tenant_name": "tenant_name",
                "prompt_parts": [
                    {"category": "category1", "content": "part1"},
                    {"category": "category2", "content": "part2"},
                ],
                "prompt_type": "text",
            }
        }


class PromptPart(EmbeddedDocument):
    """A part of a structured prompt, stored in Mongo."""
    category = StringField(required=True)
    content = StringField(required=True)
    always = BooleanField(default=True)
    embedding_vector = ListField(FloatField(), default=[])


class PromptHistory(Document):
    """A saved version of a prompt (written on every PromptDocument.save)."""
    prompt_document = ReferenceField("PromptDocument", required=True)
    name = StringField(required=True)
    tenant_name = StringField(required=True)
    content = StringField(required=False)
    prompt_parts = ListField(EmbeddedDocumentField(PromptPart), default=[])
    prompt_type = StringField(required=True, choices=["text", "structured"], default="text")
    modified_at = DateTimeField(default=datetime.now(timezone.utc))
    modified_by = StringField(required=True)

    def to_dict(self):
        """History entry as a dict for the API."""
        return {
            "id": str(self.id),
            "name": self.name,
            "tenant_name": self.tenant_name,
            "content": self.content,
            "prompt_parts": [
                {"category": part.category, "content": part.content, "always": part.always}
                for part in self.prompt_parts
            ],
            "prompt_type": self.prompt_type,
            "modified_at": self.modified_at,
            "modified_by": self.modified_by,
        }


class PromptHistoryModel(BaseModel):
    """Pydantic view of PromptHistory."""
    name: str
    tenant_name: str
    content: Optional[str]
    prompt_parts: List[PromptPartModel] = []
    prompt_type: str = "text"
    modified_at: Optional[datetime]
    modified_by: Optional[str]


class PromptDocument(Document):
    """A prompt of a tenant: default (chat), platform_realtime (phone), contextualize_q_system_prompt, hangup_call or
    any named extra; text or structured (parts).
    """
    name = StringField(required=True)
    label = StringField(required=False)
    tenant_name = StringField(required=True)
    content = StringField(required=False)
    prompt_parts = ListField(EmbeddedDocumentField(PromptPart), default=[])
    max_embedding_filter_parts = IntField(default=3)
    prompt_type = StringField(required=True, choices=["text", "structured"], default="text")
    folders_for_tags = ListField(StringField(), default=[])
    modified_at = DateTimeField(default=datetime.now(timezone.utc))
    modified_by = StringField(required=True, default=os.getenv("MAIA_DOCS_USER"))

    meta = {"collection": "prompts"}

    @property
    def prompt(self):
        """
        Returns the full prompt based on the prompt_type.
        """
        if self.prompt_type == "text":
            return self.content or "\n".join(part.content for part in self.prompt_parts)
        elif self.prompt_type == "structured":
            if len(self.prompt_parts) == 0:
                return self.content or ""
            return "\n".join(part.content for part in self.prompt_parts if part.always)
        return ""

    def save(self, *args, **kwargs):
        """Stamp modified_at, save, and append a PromptHistory version."""
        self.modified_at = datetime.now(timezone.utc)
        super(PromptDocument, self).save(*args, **kwargs)
        PromptHistory(
            prompt_document=self,
            name=self.name,
            tenant_name=self.tenant_name,
            content=self.content,
            prompt_parts=self.prompt_parts,
            prompt_type=self.prompt_type,
            modified_at=self.modified_at,
            modified_by=self.modified_by,
        ).save()

    def to_dict(self):
        """Prompt as a dict for the API (a text prompt is exposed as one Default part)."""
        if self.prompt_type == "text" and len(self.prompt_parts) == 0:
            self.prompt_parts = [PromptPart(category="Default", content=self.content, always=True)]
        return {
            "id": str(self.id),
            "name": self.name,
            "tenant_name": self.tenant_name,
            "content": self.content,
            "prompt_parts": [
                {"category": part.category, "content": part.content, "always": part.always}
                for part in self.prompt_parts
            ],
            "prompt_type": self.prompt_type,
            "folders_for_tags": self.folders_for_tags,
            "modified_at": self.modified_at,
            "modified_by": self.modified_by,
            "label": self.label or self.name,
            "prompt": self.prompt,
            "max_embedding_filter_parts": self.max_embedding_filter_parts,
        }
