"""Admin API: chat settings (human-like delay, features) and call settings (voice, speed, Realtime tuning) of a tenant."""

from fastapi import APIRouter, Depends

from maia.services.registry import get_tenant_name
from maia.models.users import User
from maia.models.settings import (
    CallSettings,
    CallSettingsDocument,
    ChatSettings,
    ChatSettingsDocument,
    FeatureSettings,
)
from maia.api.security import authenticate_user_token
from maia.services.mongo import manager_engine

settings_router = APIRouter(prefix="/settings", tags=["Settings"])


@settings_router.get("/chat/{tenant_name}", response_model=ChatSettings)
async def get_chat_settings(tenant_name: str, user: User = Depends(authenticate_user_token)):
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if chat_settings is None:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings.update_all_features()
        chat_settings.save()
        chat_settings.reload()
    chat_settings.update_all_features()
    return chat_settings


@settings_router.put("/chat/{tenant_name}", response_model=ChatSettings)
async def update_chat_settings(
    chat_settings: ChatSettings,
    tenant_name: str = Depends(get_tenant_name),
    user: User = Depends(authenticate_user_token),
):
    chat_settings_doc = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if chat_settings_doc is None:
        chat_settings_doc = ChatSettingsDocument(tenant_name=tenant_name)
    if chat_settings.chatbot_human_like_delay is not None:
        chat_settings_doc.chatbot_human_like_delay = chat_settings.chatbot_human_like_delay
    chat_settings_doc.chatbot_human_like_time = (
        chat_settings.chatbot_human_like_time or chat_settings_doc.chatbot_human_like_time
    )
    chat_settings_doc.features = [
        FeatureSettings(**feature.model_dump()) for feature in chat_settings.features
    ] or chat_settings_doc.features
    chat_settings_doc.save()
    chat_settings_doc.reload()

    return chat_settings


@settings_router.get("/call/{tenant_name}", response_model=CallSettings)
async def get_call_settings(tenant_name: str, user: User = Depends(authenticate_user_token)):
    call_settings = CallSettingsDocument.objects(tenant_name=tenant_name).first()
    if call_settings is None:
        call_settings = CallSettingsDocument(tenant_name=tenant_name)
        call_settings.update_all_features()
        call_settings.save()
        call_settings.reload()
    call_settings.update_all_features()
    return call_settings


@settings_router.put(
    "/call/{tenant_name}",
    response_model=CallSettings,
)
async def update_call_settings(
    tenant_name: str, call_settings: CallSettings, user: User = Depends(authenticate_user_token)
):
    call_settings.tenant_name = tenant_name
    # Update the tenant's existing document in place; a plain save would insert a second one
    # and the phone pipeline would keep reading the first.
    existing = await manager_engine.find_one(CallSettings, CallSettings.tenant_name == tenant_name)
    if existing is not None:
        for field, value in call_settings.model_dump(exclude={"id"}).items():
            setattr(existing, field, value)
        await manager_engine.save(existing)
        return existing
    await manager_engine.save(call_settings)
    return call_settings
