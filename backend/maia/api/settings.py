"""Admin API: chat settings (human-like delay, features) and call settings (voice, speed, Realtime tuning) of a tenant."""

from fastapi import APIRouter, Depends

from maia.services.registry import resolve_existing_tenant_name
from maia.models.users import User
from maia.models.settings import (
    CallSettings,
    CallSettingsDocument,
    ChatSettings,
    ChatSettingsDocument,
    FeatureSettings,
)
from maia.api.security import authenticate_current_user
from maia.services.mongo import odmantic_engine

settings_router = APIRouter(prefix="/settings", tags=["Settings"])


@settings_router.get("/chat/{tenant_name}", response_model=ChatSettings)
async def fetch_tenant_chat_settings(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """Chat settings of the tenant, created with the default features when missing."""
    chat_settings_doc = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if chat_settings_doc is None:
        chat_settings_doc = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings_doc.backfill_feature_defaults()
        chat_settings_doc.save()
        chat_settings_doc.reload()
    chat_settings_doc.backfill_feature_defaults()
    return chat_settings_doc


def _find_or_build_chat_settings_doc(tenant_name: str) -> ChatSettingsDocument:
    """The tenant's stored chat settings, or a new unsaved document when there are none."""
    chat_settings_doc = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if chat_settings_doc is None:
        chat_settings_doc = ChatSettingsDocument(tenant_name=tenant_name)
    return chat_settings_doc


def _merge_chat_settings_into_doc(chat_settings_doc: ChatSettingsDocument, chat_settings_payload: ChatSettings) -> None:
    """Copy the payload onto the document, in place (the caller saves): the delay flag unless it is None, the delay
    time unless it is 0, the features unless the list is empty.
    """
    if chat_settings_payload.chatbot_human_like_delay is not None:
        chat_settings_doc.chatbot_human_like_delay = chat_settings_payload.chatbot_human_like_delay
    chat_settings_doc.chatbot_human_like_time = (
        chat_settings_payload.chatbot_human_like_time or chat_settings_doc.chatbot_human_like_time
    )
    chat_settings_doc.features = [
        FeatureSettings(**incoming_feature.model_dump()) for incoming_feature in chat_settings_payload.features
    ] or chat_settings_doc.features


@settings_router.put("/chat/{tenant_name}", response_model=ChatSettings)
async def update_tenant_chat_settings(
    chat_settings_payload: ChatSettings,
    tenant_name: str = Depends(resolve_existing_tenant_name),
    current_user: User = Depends(authenticate_current_user),
):
    """Update the human-like delay and the feature flags of the tenant's chat settings."""
    chat_settings_doc = _find_or_build_chat_settings_doc(tenant_name)
    _merge_chat_settings_into_doc(chat_settings_doc, chat_settings_payload)
    chat_settings_doc.save()
    chat_settings_doc.reload()

    return chat_settings_payload


@settings_router.get("/call/{tenant_name}", response_model=CallSettings)
async def fetch_tenant_call_settings(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """Call settings of the tenant, created with the defaults when missing."""
    call_settings_doc = CallSettingsDocument.objects(tenant_name=tenant_name).first()
    if call_settings_doc is None:
        call_settings_doc = CallSettingsDocument(tenant_name=tenant_name)
        call_settings_doc.save()
        call_settings_doc.reload()
    return call_settings_doc


async def _overwrite_call_settings_in_place(
    stored_call_settings: CallSettings, call_settings_payload: CallSettings
) -> CallSettings:
    """Copy every payload field except the id onto the stored record, save it through odmantic and return it."""
    for field_name, field_value in call_settings_payload.model_dump(exclude={"id"}).items():
        setattr(stored_call_settings, field_name, field_value)
    await odmantic_engine.save(stored_call_settings)
    return stored_call_settings


@settings_router.put(
    "/call/{tenant_name}",
    response_model=CallSettings,
)
async def update_tenant_call_settings(
    tenant_name: str, call_settings_payload: CallSettings, current_user: User = Depends(authenticate_current_user)
):
    """Update the tenant's call settings in place through odmantic (a plain save would insert a second document)."""
    call_settings_payload.tenant_name = tenant_name
    # Update the tenant's existing document in place; a plain save would insert a second one
    # and the phone pipeline would keep reading the first.
    stored_call_settings = await odmantic_engine.find_one(CallSettings, CallSettings.tenant_name == tenant_name)
    if stored_call_settings is not None:
        return await _overwrite_call_settings_in_place(stored_call_settings, call_settings_payload)
    await odmantic_engine.save(call_settings_payload)
    return call_settings_payload
