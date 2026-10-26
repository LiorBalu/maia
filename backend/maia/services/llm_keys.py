"""Centralised resolution of LLM provider API keys.

Each `resolve_*_api_key()` returns the per-tenant key when set, otherwise falls
back to the platform-provided shared key (`USER_<PROVIDER>_API_KEY` env var).
"""

from __future__ import annotations

import os
from typing import Optional


def resolve_openai_api_key(tenant_document) -> Optional[str]:
    """Per-tenant OpenAI key with fallback to the platform shared key."""
    settings_key = None
    if tenant_document and tenant_document.openai_settings:
        settings_key = tenant_document.openai_settings.openai_api_key
    return settings_key or os.environ.get("MAIA_OPENAI_API_KEY") or None


def resolve_anthropic_api_key(tenant_document) -> Optional[str]:
    """Per-tenant Anthropic key with fallback to the platform shared key."""
    settings_key = None
    if tenant_document and getattr(tenant_document, "anthropic_settings", None):
        settings_key = tenant_document.anthropic_settings.anthropic_api_key
    return settings_key or os.environ.get("MAIA_ANTHROPIC_API_KEY") or None


def resolve_google_api_key(tenant_document) -> Optional[str]:
    """Per-tenant Google LLM key with fallback to the platform shared key."""
    settings_key = None
    if tenant_document and getattr(tenant_document, "google_llm_settings", None):
        settings_key = tenant_document.google_llm_settings.google_api_key
    return settings_key or os.environ.get("MAIA_GOOGLE_API_KEY") or None
