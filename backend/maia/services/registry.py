"""Per-tenant service registry (chatbot, tenant document) and the sender dispatch per platform."""

import logging
from os import getenv
from typing import Dict, Optional

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from maia.services.llm_keys import resolve_openai_api_key
from maia.models.tenants import TenantDocument
from maia.services.chatbot import ChatbotService
from maia.services.email_platform import EmailPlatformService
from maia.services.mongo import MongoService
from maia.services.waha import WahaService

logger = logging.getLogger(__name__)

client_mongo: AsyncIOMotorClient = AsyncIOMotorClient(getenv("MONGO_URI", "mongodb://localhost:27017"))
services: Dict[str, Dict[str, object]] = {}
tenants_name_list = []
DB_NAME = getenv("MAIA_DB_NAME", "maia")


def get_tenant_name(tenant_name: str):
    if tenant_name in tenants_name_list:
        return tenant_name
    tenants = TenantDocument.objects(name=tenant_name)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found")
    tenants_name_list.append(tenant_name)
    return tenant_name


def create_mongo_service(tenant_name) -> MongoService:
    return MongoService(db_name=DB_NAME)


def create_tenant_document(tenant_name) -> TenantDocument:
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        tenant = TenantDocument(name=tenant_name)
        tenant.save()  # if needed? i dont think so
    tenant.reload()
    return tenant


def update_tenant_document(tenant_name):
    if tenant_name not in services:
        services[tenant_name] = {}
    services[tenant_name]["tenant_document"] = TenantDocument.objects(name=tenant_name).first()
    logger.info("Tenant document updated")


def create_chatbot_service(tenant_name) -> ChatbotService:
    tenant_document = get_services(tenant_name, "tenant_document")
    openai_settings = tenant_document.openai_settings
    gpt_model = tenant_document.openai_settings.gpt_model or tenant_document.gpt_model

    openai_api_key = resolve_openai_api_key(tenant_document)
    return ChatbotService(
        tenant_name=tenant_name, gpt_model=gpt_model, api_key=openai_api_key, openai_settings=openai_settings
    )


SERVICE_FACTORIES = {
    "mongo_service": create_mongo_service,
    "tenant_document": create_tenant_document,
    "chatbot_service": create_chatbot_service,
}


def get_services(tenant_name: str, service_name: str, **kwargs):
    if tenant_name not in services:
        services[tenant_name] = {}
    tenant_services = services[tenant_name]
    # reload tenant_document each time but another service only once
    if service_name not in tenant_services or service_name == "tenant_document":
        if service_name not in SERVICE_FACTORIES:
            raise ValueError(f"Service inconnu : {service_name}")
        if service_name == "chatbot_service":
            return create_chatbot_service(tenant_name)

        tenant_services[service_name] = SERVICE_FACTORIES[service_name](tenant_name)
    return tenant_services[service_name]


def get_sender_service(tenant_name: str, service_name: Optional[str] = None, platform: str = "waha", **kwargs):
    if platform == "email":
        return EmailPlatformService(tenant_name, **kwargs)
    elif platform == "waha":
        phone_number = kwargs.get("to")
        return WahaService(tenant_name, phone_number=phone_number, **kwargs)


# ---------------------------------------------------------------- typed accessors (use these)
def chatbot_for(tenant_name: str) -> ChatbotService:
    """The tenant's OpenAI chat service (model and key from its settings); a fresh instance each call."""
    return get_services(tenant_name, "chatbot_service")
