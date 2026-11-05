"""Web chat (the dashboard's test chat and the embeddable widget): save the message and answer synchronously."""

import logging
import json
import uuid
from datetime import datetime, timezone

from maia.inbound.save.common import (
    SaveResponse,
    ensure_tenant_chat_settings,
    extract_text_from_attachment,
    download_and_transcribe_voice_note,
    fill_missing_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.utils.google_cloud_storage.utils.save_fastapi_file_in_bucket import upload_fastapi_file_to_bucket

logger = logging.getLogger(__name__)


def _collect_form_fields(web_form) -> dict:
    """Every posted field as a plain dict (one value per field name, the one web_form.get returns)."""
    form_fields = {}
    for field_name in web_form.keys():
        form_fields[field_name] = web_form.get(field_name)
    return form_fields


async def _resolve_media_type_and_url(form_fields, sender_id, tenant_name):
    """(message_type, media_url) of the posted message: the MessageType / MediaUrl fields, replaced by the bucket
    upload under <tenant_name>/customers/<sender_id> when a file was posted.
    """
    uploaded_file = form_fields.get("file")
    media_url = form_fields.get("MediaUrl", None)
    message_type = form_fields.get("MessageType", "text")
    if uploaded_file:
        message_type, media_url = await upload_fastapi_file_to_bucket(
            f"{tenant_name}/customers/{sender_id}", uploaded_file
        )
    return message_type, media_url


def _verify_tenant_is_active(tenant_name):
    """Raise ValueError when the tenant does not exist or is inactive."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        raise ValueError(f"Tenant {tenant_name} not found")
    if tenant_doc.status == "inactive":
        raise ValueError(f"Tenant {tenant_name} is inactive")


def _compose_message_body(form_fields, media_url, message_type, tenant_name) -> str:
    """The message text: the Body field, replaced by the voice-note transcript when there is one, then the text read
    from an attached file appended after a blank line.
    """
    message_body = form_fields.get("Body", "")
    if audio_transcript := download_and_transcribe_voice_note(media_url, message_type, tenant_name):
        message_body = audio_transcript
    chat_settings_doc = ensure_tenant_chat_settings(tenant_name)

    attachment_text = extract_text_from_attachment(
        media_url, message_type, form_fields.get("MediaContentType0", ""), chat_settings_doc
    )
    if attachment_text:
        message_body += f"\n\n{attachment_text}"
    return message_body


def _persist_web_message_doc(customer_doc, message_body, message_type, media_url, tenant_name) -> MessageDocument:
    """Build and save the customer's MessageDocument (platform web, role user, to bot, a fresh sid)."""
    # Create message document
    message_doc = MessageDocument(
        tenant_name=tenant_name,
        platform="web",
        phone_number=customer_doc.phone_number,
        sender_id=str(customer_doc.id),
        profile_name=customer_doc.name,
        message=message_body,
        role="user",
        to="bot",
        msg_type=message_type,
        media_url=media_url,
        sid=str(uuid.uuid4()),
        message_sids=[],
        date=datetime.now(timezone.utc),
    )
    message_doc.save()  # Save the message to database
    return message_doc


def _log_empty_message(form_fields):
    """Log that the message is empty, with the posted fields (as JSON when they serialize, raw otherwise)."""
    logger.info("Empty message, not processing further")
    try:
        logger.info(json.dumps(form_fields, indent=4, ensure_ascii=False))
    except Exception:
        logger.error(form_fields)


async def save_web_message(web_form, tenant_name) -> SaveResponse:
    """Save incoming message from Web API"""
    form_fields = _collect_form_fields(web_form)
    sender_id = form_fields.get("From", str(uuid.uuid4()))
    message_type, media_url = await _resolve_media_type_and_url(form_fields, sender_id, tenant_name)

    _verify_tenant_is_active(tenant_name)
    customer_doc = _ensure_web_customer(sender_id, tenant_name)
    message_body = _compose_message_body(form_fields, media_url, message_type, tenant_name)
    # Handle the reply modes
    if customer_doc.reply_mode == "blocked":
        return SaveResponse()

    message_doc = _persist_web_message_doc(customer_doc, message_body, message_type, media_url, tenant_name)
    fill_missing_conversation_profile_name(
        tenant_name, customer_doc.phone_number, customer_doc.name, message_doc.conversation_id
    )
    if not message_body:
        _log_empty_message(form_fields)
    return SaveResponse(
        tenant_name=tenant_name,
        sender_id=customer_doc.phone_number,
        reply_delay_seconds=0,
        triggering_message_id=str(message_doc.id),
        conversation_id=message_doc.conversation_id,
    )


def _ensure_web_customer(sender_id, tenant_name):
    """The customer of a web chat, keyed by the From field (a uuid for the dashboard chat); created when missing."""
    customer_doc = CustomerDocument.objects(phone_number=sender_id, tenant_name=tenant_name).first()
    if not customer_doc:
        customer_doc = CustomerDocument(
            tenant_name=tenant_name,
            phone_number=sender_id,
            name=sender_id,
            created_date=datetime.now(timezone.utc).replace(microsecond=0),
        )
        customer_doc.save()
        customer_doc.reload()
    return customer_doc
