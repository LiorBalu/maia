"""Per-tenant service registry (chatbot, tenant document) and the sender dispatch per platform."""

import logging
from os import getenv
from typing import Dict, Optional

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from maia.services.llm_keys import resolve_openai_api_key_for_tenant
from maia.models.tenants import TenantDocument
from maia.services.chatbot import ChatbotService
from maia.services.email_platform import EmailPlatformService
from maia.services.mongo import MongoService
from maia.services.waha import WahaService

logger = logging.getLogger(__name__)

_motor_client: AsyncIOMotorClient = AsyncIOMotorClient(getenv("MONGO_URI", "mongodb://localhost:27017"))
_services_by_tenant: Dict[str, Dict[str, object]] = {}
_known_tenant_names = []
_MONGO_DATABASE_NAME = getenv("MAIA_DB_NAME", "maia")


def resolve_existing_tenant_name(tenant_name: str):
    """FastAPI dependency: the tenant name when the tenant exists (cached), 404 otherwise."""
    if tenant_name in _known_tenant_names:
        return tenant_name
    matching_tenants = TenantDocument.objects(name=tenant_name)
    if not matching_tenants:
        raise HTTPException(status_code=404, detail="Tenant not found")
    _known_tenant_names.append(tenant_name)
    return tenant_name


def _build_mongo_service(_tenant_name) -> MongoService:
    """MongoService on the platform database."""
    return MongoService(db_name=_MONGO_DATABASE_NAME)


def _ensure_tenant_doc(tenant_name) -> TenantDocument:
    """The tenant document, created when missing."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        tenant_doc = TenantDocument(name=tenant_name)
        tenant_doc.save()  # if needed? i dont think so
    tenant_doc.reload()
    return tenant_doc


def refresh_cached_tenant_doc(tenant_name):
    """Reload the tenant document into the registry cache."""
    if tenant_name not in _services_by_tenant:
        _services_by_tenant[tenant_name] = {}
    _services_by_tenant[tenant_name]["tenant_document"] = TenantDocument.objects(name=tenant_name).first()
    logger.info("Tenant document updated")


def _build_chatbot_from_tenant_settings(tenant_name) -> ChatbotService:
    """ChatbotService for the tenant, from its OpenAI settings (or the shared key)."""
    tenant_doc = _resolve_tenant_service(tenant_name, "tenant_document")
    tenant_openai_settings = tenant_doc.openai_settings
    chat_model_name = tenant_doc.openai_settings.gpt_model or tenant_doc.gpt_model

    effective_openai_api_key = resolve_openai_api_key_for_tenant(tenant_doc)
    return ChatbotService(
        tenant_name=tenant_name,
        gpt_model=chat_model_name,
        api_key=effective_openai_api_key,
        openai_settings=tenant_openai_settings,
    )


_FACTORY_BY_SERVICE_KEY = {
    "mongo_service": _build_mongo_service,
    "tenant_document": _ensure_tenant_doc,
    "chatbot_service": _build_chatbot_from_tenant_settings,
}


def _resolve_tenant_service(tenant_name: str, service_key: str, **ignored_options):
    """Per-tenant service cache: tenant_document is reloaded on every call, chatbot_service is always fresh, the
    others are created once.
    """
    if tenant_name not in _services_by_tenant:
        _services_by_tenant[tenant_name] = {}
    cached_tenant_services = _services_by_tenant[tenant_name]
    # reload tenant_document each time but another service only once
    if service_key not in cached_tenant_services or service_key == "tenant_document":
        if service_key not in _FACTORY_BY_SERVICE_KEY:
            raise ValueError(f"Service inconnu : {service_key}")
        if service_key == "chatbot_service":
            return _build_chatbot_from_tenant_settings(tenant_name)

        cached_tenant_services[service_key] = _FACTORY_BY_SERVICE_KEY[service_key](tenant_name)
    return cached_tenant_services[service_key]


def build_platform_sender_service(tenant_name: str, service_name: Optional[str] = None, platform: str = "waha", **sender_options):
    """The outbound service of a platform: EmailPlatformService for email, WahaService (keyed by the business number
    in ``to``) for WhatsApp.
    """
    if platform == "email":
        return EmailPlatformService(tenant_name, **sender_options)
    elif platform == "waha":
        business_phone_number = sender_options.get("to")
        return WahaService(tenant_name, phone_number=business_phone_number, **sender_options)


# ---------------------------------------------------------------- typed accessors (use these)
def build_tenant_chatbot(tenant_name: str) -> ChatbotService:
    """The tenant's OpenAI chat service (model and key from its settings); a fresh instance each call."""
    return _resolve_tenant_service(tenant_name, "chatbot_service")
