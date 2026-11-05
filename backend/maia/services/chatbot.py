"""ChatbotService: the tenant's OpenAI client for the calls made outside the RAG chain (voice-note transcription) and the Realtime WebSocket URL."""

import logging

from openai import OpenAI

from maia.models.tenants import OpenAISettingsModel

logger = logging.getLogger(__name__)

OPENAI_REALTIME_WEBSOCKET_BASE_URL = "wss://api.openai.com/v1/realtime"


class ChatbotService:
    """OpenAI client of a tenant (its own key, or the platform's shared one)."""
    def __init__(self, tenant_name: str, gpt_model: str, api_key: str, openai_settings: OpenAISettingsModel):
        """Keep the model, the key and the settings; open the OpenAI client."""
        self.gpt_model = gpt_model
        self.api_key = api_key
        self.client = OpenAI(api_key=api_key)
        self.tenant_name = tenant_name
        self.openai_settings = openai_settings

    @staticmethod
    def build_realtime_websocket_url(realtime_model_name: str) -> str:
        """WebSocket URL of the OpenAI Realtime API for a model."""
        return f"{OPENAI_REALTIME_WEBSOCKET_BASE_URL}?model={realtime_model_name}"
