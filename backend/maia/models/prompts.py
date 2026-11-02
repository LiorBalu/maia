"""Prompt documents of a tenant (`default`, `platform_realtime`, named extras)."""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from mongoengine import (
    DateTimeField,
    Document,
    StringField,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Prompt(BaseModel):
    """Prompt payload of the API (create / update / read)."""
    name: str
    tenant_name: str
    prompt: Optional[str] = Field(
        default=None,
        description="**The prompt text (read only; the same as `content`).**",
    )
    content: Optional[str] = None
    modified_at: Optional[datetime] = datetime.now(timezone.utc)
    modified_by: Optional[str] = os.getenv("MAIA_DOCS_USER")
    label: Optional[str] = "ראשי"

    class Config:
        """Example payload shown in the API docs."""
        json_schema_extra = {
            "example": {
                "name": "prompt_name",
                "tenant_name": "tenant_name",
                "content": "prompt text",
            }
        }


class PromptDocument(Document):
    """A prompt of a tenant: default (chat), platform_realtime (phone), contextualize_q_system_prompt, hangup_call or
    any named extra; the text is `content`.
    """
    name = StringField(required=True)
    label = StringField(required=False)
    tenant_name = StringField(required=True)
    content = StringField(required=False)
    modified_at = DateTimeField(default=datetime.now(timezone.utc))
    modified_by = StringField(required=True, default=os.getenv("MAIA_DOCS_USER"))

    meta = {"collection": "prompts"}

    @property
    def prompt(self):
        """The prompt text (`content`, or "" when empty)."""
        return self.content or ""

    def save(self, *args, **kwargs):
        """Stamp modified_at and save."""
        self.modified_at = datetime.now(timezone.utc)
        return super(PromptDocument, self).save(*args, **kwargs)

    def to_dict(self):
        """Prompt as a dict for the API."""
        return {
            "id": str(self.id),
            "name": self.name,
            "tenant_name": self.tenant_name,
            "content": self.content,
            "modified_at": self.modified_at,
            "modified_by": self.modified_by,
            "label": self.label or self.name,
            "prompt": self.prompt,
        }
