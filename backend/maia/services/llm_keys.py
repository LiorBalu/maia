"""Centralised resolution of LLM provider API keys.

Each `resolve_*_api_key_for_tenant()` returns the per-tenant key when set, otherwise falls
back to the platform-provided shared key (`MAIA_<PROVIDER>_API_KEY` env var).
"""

from __future__ import annotations

import os
from typing import Optional


def resolve_openai_api_key_for_tenant(tenant_doc) -> Optional[str]:
    """Per-tenant OpenAI key with fallback to the platform shared key."""
    tenant_api_key = None
    if tenant_doc and tenant_doc.openai_settings:
        tenant_api_key = tenant_doc.openai_settings.openai_api_key
    return tenant_api_key or os.environ.get("MAIA_OPENAI_API_KEY") or None


def resolve_anthropic_api_key_for_tenant(tenant_doc) -> Optional[str]:
    """Per-tenant Anthropic key with fallback to the platform shared key."""
    tenant_api_key = None
    if tenant_doc and getattr(tenant_doc, "anthropic_settings", None):
        tenant_api_key = tenant_doc.anthropic_settings.anthropic_api_key
    return tenant_api_key or os.environ.get("MAIA_ANTHROPIC_API_KEY") or None


def resolve_google_api_key_for_tenant(tenant_doc) -> Optional[str]:
    """Per-tenant Google LLM key with fallback to the platform shared key."""
    tenant_api_key = None
    if tenant_doc and getattr(tenant_doc, "google_llm_settings", None):
        tenant_api_key = tenant_doc.google_llm_settings.google_api_key
    return tenant_api_key or os.environ.get("MAIA_GOOGLE_API_KEY") or None
