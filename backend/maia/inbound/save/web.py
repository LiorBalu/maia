"""Web chat (the dashboard's test chat and the embeddable widget): save the message and answer synchronously."""

import logging
import json
import uuid
from datetime import datetime, timezone

from maia.inbound.save.common import (
    SaveResponse,
    get_or_create_chat_settings,
    extract_text_from_media,
    handle_audio_message,
    update_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.utils.google_cloud_storage.utils.save_fastapi_file_in_bucket import save_fastapi_file_in_bucket

logger = logging.getLogger(__name__)


async def save_web_input(form, tenant_name) -> SaveResponse:
    """Save incoming message from Web API"""
    file = form.get("file")
    data = {}
    for key in form.keys():
        data[key] = form.get(key)
    From = data.get("From", str(uuid.uuid4()))
    media_url = data.get("MediaUrl", None)
    message_type = data.get("MessageType", "text")
    if file:
        message_type, media_url = await save_fastapi_file_in_bucket(f"{tenant_name}/customers/{From}", file)

    # Create message document
    # Handeling incoming message
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise ValueError(f"Tenant {tenant_name} not found")
    if tenant.status == "inactive":
        raise ValueError(f"Tenant {tenant_name} is inactive")
    customer = create_or_get_customer(From, tenant_name)
    body_message = data.get("Body", "")
    if transcoded_audio := handle_audio_message(media_url, message_type, tenant_name):
        body_message = transcoded_audio
    chat_settings = get_or_create_chat_settings(tenant_name)

    media_text = extract_text_from_media(media_url, message_type, form.get("MediaContentType0", ""), chat_settings)
    if media_text:
        body_message += f"\n\n{media_text}"
    # Handle the reply modes
    if customer.reply_mode == "blocked":
        return SaveResponse()

    msg = MessageDocument(
        tenant_name=tenant_name,
        platform="web",
        phone_number=customer.phone_number,
        sender_id=str(customer.id),
        profile_name=customer.name,
        message=body_message,
        role="user",
        to="bot",
        msg_type=message_type,
        media_url=media_url,
        sid=str(uuid.uuid4()),
        message_sids=[],
        date=datetime.now(timezone.utc),
    )
    msg.save()  # Save the message to database
    update_conversation_profile_name(tenant_name, customer.phone_number, customer.name, msg.conversation_id)
    if not body_message:
        logger.info("Empty message, not processing further")
        try:
            logger.info(json.dumps(data, indent=4, ensure_ascii=False))
        except Exception:
            logger.error(data)
    return SaveResponse(
        tenant_name=tenant_name,
        user_id=customer.phone_number,
        delay=0,
        task_message_id=str(msg.id),
        conversation_id=msg.conversation_id,
    )


def create_or_get_customer(from_number, tenant_name):
    """The customer of a web chat, keyed by the From field (a uuid for the dashboard chat); created when missing."""
    chat_id = from_number
    customer = CustomerDocument.objects(phone_number=chat_id, tenant_name=tenant_name).first()
    if not customer:
        customer = CustomerDocument(
            tenant_name=tenant_name,
            phone_number=chat_id,
            name=chat_id,
            created_date=datetime.now(timezone.utc).replace(microsecond=0),
        )
        customer.save()
        customer.reload()
    return customer
