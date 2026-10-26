"""AgentDocument: per-tenant LLM configuration (provider, model, temperature, top-k) read by the RAG chain as `agent_config`."""

from datetime import datetime

from mongoengine import BooleanField, DateTimeField, Document, FloatField, IntField, StringField
from pydantic import BaseModel


class AgentDocument(Document):
    name = StringField(required=True)
    tenant_name = StringField(required=True)
    description = StringField()
    is_active = BooleanField(default=True)
    model = StringField(default="gpt-4.1-mini")
    provider = StringField(default="openai")
    temperature = FloatField(default=0.3)
    score_threshold = FloatField(default=0.5)
    top_k_general = IntField(default=10)
    top_k_qa = IntField(default=5)
    direct_answer_threshold = FloatField(default=0)
    created_at = DateTimeField()
    updated_at = DateTimeField()
    meta = {"collection": "agents"}

    def save(self, *args, **kwargs):
        if not self.created_at:
            self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        if self.is_active:
            # Deactivate other agents in the same tenant
            AgentDocument.objects(tenant_name=self.tenant_name, is_active=True, id__ne=self.id).update(
                set__is_active=False
            )
        return super().save(*args, **kwargs)

    @property
    def agent_config(self):
        return {
            "model": self.model,
            "provider": self.provider,
            "temperature": self.temperature,
            "score_threshold": self.score_threshold,
            "top_k_general": self.top_k_general,
            "top_k_qa": self.top_k_qa,
            "direct_answer_threshold": self.direct_answer_threshold,
        }


class AgentCreateRequest(BaseModel):
    name: str
    description: str
    is_active: bool
    model: str = "gpt-4.1-mini"
    provider: str = "openai"
    temperature: float = 0.3
    score_threshold: float = 0.5
    top_k_general: int = 10
    top_k_qa: int = 5
    direct_answer_threshold: float = 0


class AgentResponse(BaseModel):
    id: str
    tenant_name: str
    name: str
    description: str
    is_active: bool
    model: str
    provider: str
    score_threshold: float
    top_k_general: int
    top_k_qa: int
    direct_answer_threshold: float
    temperature: float
    created_at: int
    updated_at: int
