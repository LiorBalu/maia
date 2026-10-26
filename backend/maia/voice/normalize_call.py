"""Turns Twilio's Voice webhook form into the pipeline's MessageContext."""

import logging
import uuid

from maia.pipeline.context import MessageContext, TenantNotFound
from maia.models.customers import fetch_or_create_customer
from maia.models.tenants import TenantDocument
from maia.models.settings import CallSettingsDocument, ChatSettingsDocument
from maia.utils.conversation.get_or_create import get_or_create_online_conversation

logger = logging.getLogger(__name__)


def normalize_call_data(data: dict, tenant_name: str) -> MessageContext:
    """Turn Twilio's Voice webhook form into the pipeline's ``MessageContext``.

    Needs Twilio's ``Caller`` and ``Called`` (a ``KeyError`` otherwise). Raises ``TenantNotFound``
    when the tenant does not exist.
    """
    logger.info("normalize_call_data")
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        logger.warning(f"Tenant {tenant_name} not found")
        raise TenantNotFound()
    caller = data["Caller"].replace("+", "")
    called_number = data["Called"].replace("+", "")
    call_settings = CallSettingsDocument.objects(tenant_name=tenant_name).first()
    if caller == call_settings.phone_number:
        # Outbound call placed by the business: the customer is the called side.
        called_number = caller
        caller = data["Called"].replace("+", "")
    customer = fetch_or_create_customer(
        number_phone=caller,
        tenant_name=tenant_name,
        profile_name=caller,
        platform=caller,
    )
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    call_sid = data.get("CallSid")
    conversation = get_or_create_online_conversation(tenant_name, caller, "telephone")
    conversation.meta_data.update({"call_sid": call_sid, "called_number": called_number})
    conversation.save()
    return MessageContext(
        tenant_name=tenant_name,
        platform="telephone",
        customer=customer,
        tenant=tenant,
        conversation=conversation,
        chat_settings=chat_settings,
        sender_id=caller,
        recipient_id=called_number,
        sender_name=data["Caller"],
        message_text=data.get("SpeechResult", ""),
        message_id=str(uuid.uuid4()),
        message_type="text",
        media_url=None,
        media_content_type=None,
        extra_prompt_name=None,
        additional_context="",
        meta_data={"call_sid": call_sid},
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )
