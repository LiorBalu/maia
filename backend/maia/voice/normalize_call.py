"""Turns Twilio's Voice webhook form into the pipeline's MessageContext."""

import logging
import uuid

from maia.pipeline.context import MessageContext, TenantNotFound
from maia.models.conversations import Conversation
from maia.models.customers import ensure_customer
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument
from maia.utils.conversation.get_or_create import ensure_online_conversation

logger = logging.getLogger(__name__)


def _load_tenant_doc(tenant_name: str) -> TenantDocument:
    """The tenant's document; logs a warning and raises ``TenantNotFound`` when the tenant does not exist."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        logger.warning(f"Tenant {tenant_name} not found")
        raise TenantNotFound()
    return tenant_doc


def _ensure_call_conversation(
    tenant_name: str, caller_number: str, business_number: str, twilio_call_sid: str | None
) -> Conversation:
    """Get or create the caller's online telephone conversation, record the call on it (``call_sid`` and
    ``called_number`` in ``meta_data``) and save it (the save also publishes the conversation to the live inbox).
    """
    call_conversation = ensure_online_conversation(tenant_name, caller_number, "telephone")
    call_conversation.meta_data.update({"call_sid": twilio_call_sid, "called_number": business_number})
    call_conversation.save()
    return call_conversation


def build_call_message_context(webhook_payload: dict, tenant_name: str) -> MessageContext:
    """Turn Twilio's Voice webhook form into the pipeline's ``MessageContext``.

    Needs Twilio's ``Caller`` and ``Called`` (a ``KeyError`` otherwise). Raises ``TenantNotFound``
    when the tenant does not exist.
    """
    logger.info("build_call_message_context")
    tenant_doc = _load_tenant_doc(tenant_name)
    caller_number = webhook_payload["Caller"].replace("+", "")
    business_number = webhook_payload["Called"].replace("+", "")
    customer_doc = ensure_customer(
        phone_or_email=caller_number,
        tenant_name=tenant_name,
        customer_profile_name=caller_number,
        platform=caller_number,
    )
    chat_settings_doc = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    twilio_call_sid = webhook_payload.get("CallSid")
    call_conversation = _ensure_call_conversation(tenant_name, caller_number, business_number, twilio_call_sid)
    return MessageContext(
        tenant_name=tenant_name,
        platform="telephone",
        customer=customer_doc,
        tenant=tenant_doc,
        conversation=call_conversation,
        chat_settings=chat_settings_doc,
        sender_id=caller_number,
        recipient_id=business_number,
        sender_name=webhook_payload["Caller"],
        message_text=webhook_payload.get("SpeechResult", ""),
        message_id=str(uuid.uuid4()),
        message_type="text",
        media_url=None,
        media_content_type=None,
        meta_data={"call_sid": twilio_call_sid},
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )
