"""Ends a conversation: closes it, clears the customer's agent flag, saves the call record for phone calls."""

import logging
from datetime import datetime, timezone
from io import BytesIO

import requests

from maia.models.customers import CustomerDocument
from maia.models.conversations import Conversation, online_conversation_filter
from maia.models.tenants import TenantDocument
from maia.services.twilio_sms import TwilioSmsService
from maia.utils.google_cloud_storage.public import upload_to_gcs

logger = logging.getLogger(__name__)


def _get_reset_conversation(tenant_name: str, phone_number: str, conversation_id: str = None, platform: str = None):
    if conversation_id:
        return Conversation.objects(id=conversation_id).first()
    # Platforms are always kept in separate conversations: `platform` narrows the lookup, so a
    # call reset never clears a sibling platform's (e.g. WhatsApp) online conversation.
    return Conversation.objects(**online_conversation_filter(tenant_name, phone_number, platform)).first()


async def _finish_conversation_reset(tenant_name: str, phone_number: str, conversation_doc) -> None:
    if not conversation_doc:
        return

    update_conversation_with_call_record(tenant_name, conversation_doc)
    conversation_doc.finish_conversation()


def _reset_customer_conversation_flags(tenant_name: str, phone_number: str) -> None:
    customer = CustomerDocument.objects(tenant_name=tenant_name, phone_number=phone_number).first()
    if not customer:
        return
    customer.agent_requested = False
    customer.save()
    customer.reload()


async def reset_conversation(tenant_name: str, phone_number: str, conversation_id: str = None, platform: str = None):
    conversation_doc = _get_reset_conversation(tenant_name, phone_number, conversation_id, platform)
    await _finish_conversation_reset(tenant_name, phone_number, conversation_doc)
    _reset_customer_conversation_flags(tenant_name, phone_number)
    logger.info(f"Customer {phone_number} conversation reset.")


def update_conversation_with_call_record(tenant_name, conversation_doc):
    try:
        if (
            conversation_doc.platform == "telephone"
            and conversation_doc.meta_data.get("call_sid")
            and not conversation_doc.meta_data.get("call_record")
        ):
            logger.info(f"Updating conversation {conversation_doc.phone_number} with call record.")
            call_sid = conversation_doc.meta_data.get("call_sid")
            called_number = conversation_doc.meta_data.get("called_number", None)
            # Get the call service
            tenant = TenantDocument.objects(name=tenant_name).first()
            twilio_service = TwilioSmsService(tenant_name, tenant, sender_number=called_number)
            # Get the call record
            call_record = twilio_service.get_record_by_sid(call_sid)
            if not call_record:
                logger.info(f"No call record found for call SID: {call_sid}")
            else:
                logger.info(f"Call record found for call SID: {call_sid}")
            if call_record:
                media_url = call_record.media_url if call_record.media_url else None
                if media_url:
                    # save media in our bucket
                    response = requests.get(
                        media_url, auth=(twilio_service.twilio_account_sid, twilio_service.twilio_auth_token)
                    )
                if response.status_code == 200:
                    media_file = BytesIO(response.content)
                    gcp_media_url = upload_to_gcs(
                        media_file,
                        tenant_name,
                        datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S-%f") + ".mp3",
                        content_type="audio/mpeg",
                    )
                # add the call record to the conversation meta_data
                conversation_doc.meta_data["call_record"] = {
                    "duration": call_record.duration,
                    "status": call_record.status,
                    "media_url": gcp_media_url,
                }
                conversation_doc.save()
    except Exception as e:
        logger.error(f"Error updating conversation with call record: {e}")
