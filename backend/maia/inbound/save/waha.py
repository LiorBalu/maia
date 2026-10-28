"""WAHA (WhatsApp) webhook handler: session status events and private-chat messages (text, media, voice notes, quoted replies), saving and dispatch."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from mongoengine.errors import NotUniqueError

from maia.inbound.save.common import (
    SaveResponse,
    get_or_create_chat_settings,
    extract_text_from_media,
    handle_audio_message,
    retrieve_human_delay_setting,
    update_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.services.waha import WahaService
from maia.utils.google_cloud_storage.utils.save_file_url_in_bucket import save_file_url_in_bucket

logger = logging.getLogger(__name__)

# WhatsApp group chat ids end with this JID suffix. The bot only serves private chats.
GROUP_JID_SUFFIX = "@g.us"


async def save_waha_input(data: dict, tenant_name: Optional[str] = None) -> SaveResponse:  # NOSONAR
    """Save an incoming WAHA webhook event (private chats only).

    WAHA webhook format (NOWEB engine; WEBJS differs only inside ``_data``):
    {
        "event": "message",
        "session": "<our connected number>",
        "payload": {
            "id": "message_id",
            "timestamp": 1234567890,
            "from": "<customer>@c.us",          (or "<lid>@lid")
            "fromMe": false,
            "to": "<our number>@c.us",          (often missing on NOWEB)
            "body": "message text",
            "hasMedia": false,
            "media": {"url": "...", "mimetype": "..."},   (only with media)
            "ack": 1,
            "_data": {"key": {"id": "...", "remoteJid": "...", "remoteJidAlt": "..."}, "pushName": "..."}
        }
    }

    ``session.status`` / ``session.created`` events update the tenant's WAHA account. Group chats,
    WhatsApp Status broadcasts, reactions, polls, calls and delivery acks are ignored.
    """
    if handle_connection_status_message(data, tenant_name):
        return SaveResponse()

    event = data.get("event")
    payload = data.get("payload", {})
    # "message.ack" (delivery receipts), reactions, polls and calls are not messages
    if event not in ["message", "message.any"]:
        return SaveResponse()

    from_field = payload.get("from", "")
    # The bot does not take part in group chats and ignores WhatsApp Status broadcasts
    if GROUP_JID_SUFFIX in from_field or GROUP_JID_SUFFIX in payload.get("to", "") or from_field.startswith("status@"):
        return SaveResponse()

    from_me = payload.get("fromMe", False)

    # Early duplicate check - do this BEFORE any heavy processing to prevent race conditions.
    # WEBJS exposes the raw id at _data.id.id; NOWEB/GOWS at _data.key.id.
    # We store outgoing AI messages with that same raw id, so the fromMe echo
    # de-duplicates here instead of being re-saved as a spurious "manager" msg.
    message_sid = payload.get("_data", {}).get("id", {}).get("id") or payload.get("_data", {}).get("key", {}).get("id")
    fake_message_sid = payload.get("id", "")
    try:
        if message_sid or fake_message_sid:
            sid__in = [sid for sid in [message_sid, fake_message_sid] if sid]
            existing_msg = MessageDocument.objects(
                # search 2 options because WAHA sometimes sends different IDs
                sid__in=sid__in,
                tenant_name=tenant_name,
            ).first()
            if existing_msg:
                return SaveResponse()
            # Double check in meta_data for fake_message_sid
            existing_msg = MessageDocument.objects(meta_data__fake_sid__in=sid__in, tenant_name=tenant_name).first()
            if existing_msg:
                return SaveResponse()
    except NotUniqueError:
        return SaveResponse()

    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise ValueError(f"Tenant {tenant_name} not found")
    if tenant.status == "inactive":
        raise ValueError(f"Tenant {tenant_name} is inactive")
    chat_settings = get_or_create_chat_settings(tenant_name)
    delay = retrieve_human_delay_setting(chat_settings)

    customer = create_or_get_customer(data, tenant_name, from_me)
    body_message = prepare_body_message(payload)
    media_url, message_type = get_message_media_url(data, tenant_name)

    # Quoted reply: prepend the quoted text so the model sees what the customer answers to
    replit_message_sid = payload.get("_data", {}).get("quotedStanzaID")
    if replit_message_sid:
        quoted_body = payload.get("_data", {}).get("quotedMsg", {}).get("body", "")
        body_message = f'[Replying to: "{quoted_body}"]\n{body_message}'

    # `to` is our connected account number: the send path resolves the WAHA
    # connection with it (WahaService(tenant_name, to)). NOWEB often omits
    # payload.to; the WAHA session name *is* that number, so fall back to it.
    to_field = payload.get("to", "") if not from_me else from_field
    if "@lid" in to_field:
        to = _resolve_lid_to_phone_number(data, tenant_name, to_field)
    else:
        to = to_field.replace("@c.us", "")
    if not to:
        to = data.get("session", "").replace("+", "")

    if transcoded_audio := handle_audio_message(media_url, message_type, tenant_name):
        body_message = transcoded_audio

    # Extract text from media (images, PDFs, documents)
    if media_url and message_type in ["image", "document"]:
        mime_type = payload.get("_data", {}).get("mimetype", "")
        if not mime_type and message_type == "image":
            mime_type = "image/jpeg"
        elif not mime_type and message_type == "document":
            mime_type = "application/pdf"
        if media_text := extract_text_from_media(media_url, message_type, mime_type, chat_settings):
            body_message += f"\n\n{media_text}"

    # Determine the role based on who sent the message
    if from_me:
        # Sent by us: from the phone -> 'manager', through the WAHA API (the bot) -> 'assistant'
        role = "assistant" if payload.get("source") == "api" else "manager"
        # Don't apply delay for our own messages
        delay = 0
    else:
        role = "user"
        # Handle the reply modes
        if customer.reply_mode == "blocked":
            return SaveResponse()
        if customer.reply_mode == "agent":
            delay = 0

    # For outgoing (fromMe) chats notifyName/pushName is OUR push name, not the
    # recipient's, so the message and the conversation are named after the customer.
    profile_name = customer.name if from_me else _extract_push_name(payload, customer.phone_number)

    if not body_message and not media_url:
        # No message to save
        return SaveResponse()
    msg = MessageDocument(
        tenant_name=tenant_name,
        platform="waha",
        phone_number=customer.phone_number,
        sender_id=str(customer.id),
        profile_name=profile_name,
        message=body_message,
        role=role,
        to=to,
        msg_type=message_type,
        media_url=media_url,
        sid=message_sid or fake_message_sid or str(uuid.uuid4()),
        message_sids=[],
        date=datetime.now(timezone.utc),
        meta_data={
            "session": data.get("session", ""),
            "replit_message_sid": replit_message_sid,
            "from_me": from_me,
            "ack": payload.get("ack"),
            "timestamp": payload.get("timestamp"),
        },
    )
    try:
        msg.save()
    except NotUniqueError:
        return SaveResponse()
    update_conversation_profile_name(tenant_name, customer.phone_number, profile_name, msg.conversation_id)

    # If message is from us (fromMe=true), don't trigger AI response
    if from_me:
        return SaveResponse()
    return SaveResponse(
        tenant_name=tenant_name,
        user_id=customer.phone_number,
        delay=delay,
        task_message_id=str(msg.id),
        conversation_id=msg.conversation_id,
    )


# Status emoji mapping for session status notifications
STATUS_EMOJI = {"WORKING": "🟢", "STOPPED": "🔴", "STARTING": "🟡", "SCAN_QR_CODE": "📱", "FAILED": "🚨"}


# Hebrew status labels
STATUS_LABEL_HE = {
    "WORKING": "מחובר",
    "STOPPED": "מנותק",
    "STARTING": "מתחיל",
    "SCAN_QR_CODE": "סריקת QR",
    "FAILED": "נכשל",
}


STATUS_LABEL_EN = {
    "WORKING": "Connected",
    "STOPPED": "Stopped",
    "STARTING": "Starting",
    "SCAN_QR_CODE": "Scan QR Code",
    "FAILED": "Failed",
}


def _send_session_status_notification(tenant, tenant_name: str, session_name: str, status: str):
    """Email every user with access to the tenant about a session status change, from the tenant's connected Gmail
    account (nothing is sent when the tenant has none).
    """
    try:
        from maia.models.users import UserDocument
        from maia.services.email_platform import send_tenant_email

        # Find all users that have permissions for this tenant
        users = UserDocument.objects(**{f"permissions__{tenant_name}__exists": True})
        if not users:
            return

        lang = getattr(tenant, "default_language", "he") or "he"
        emoji = STATUS_EMOJI.get(status, "❓")
        status_label_he = STATUS_LABEL_HE.get(status, status)
        status_label_en = STATUS_LABEL_EN.get(status, status)
        status_label = status_label_he if lang == "he" else status_label_en
        tenant_name_formated = tenant_name.replace("-", " ").replace("_", " ").title()
        if lang == "he":
            subject = f"{emoji} עדכון סטטוס חיבור - {tenant_name_formated}"
            html_body = f"""
            <div dir="rtl" style="font-family: Arial, sans-serif; padding: 20px;">
                <h2>{emoji} עדכון סטטוס חיבור</h2>
                <p><strong>שם העסק:</strong> {tenant_name_formated}</p>
                <p><strong>מספר טלפון:</strong> {session_name}</p>
                <p><strong>סטטוס:</strong> {emoji} {status_label}</p>
            </div>
            """
        else:
            subject = f"{emoji} Connection Status Update - {tenant_name_formated}"
            html_body = f"""
            <div style="font-family: Arial, sans-serif; padding: 20px;">
                <h2>{emoji} Connection Status Update</h2>
                <p><strong>Tenant:</strong> {tenant_name_formated}</p>
                <p><strong>Phone Number:</strong> {session_name}</p>
                <p><strong>Status:</strong> {emoji} {status_label}</p>
            </div>
            """

        for user in users:
            if not user.email:
                continue
            try:
                send_tenant_email(tenant_name, to=user.email, subject=subject, html_body=html_body)
            except Exception as e:
                logger.error(f"Error sending email to {user.email} for {tenant_name}: {e}")

    except Exception as e:
        logger.error(f"Error sending session status notification for {tenant_name}: {e}")


def handle_connection_status_message(data, tenant_name):
    """Check if the message is a connection status message"""
    # WAHA Pro sends session.status events
    event = data.get("event")
    if event in ["session.status", "session.created"]:
        # Update tenant connection status
        tenant = TenantDocument.objects(name=tenant_name).first()
        if tenant:
            session_data = data.get("payload", {})
            session_name = session_data.get("name", "")
            status = session_data.get("status", "")
            status_history = session_data.get("statuses", [])

            waha_instance = tenant.get_waha_by_phone_number(session_name.replace("+", ""))
            if waha_instance:
                # WAHA statuses: STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED
                waha_instance.is_connected = status == "WORKING"
                waha_instance.session_status = status
                waha_instance.last_status_update = datetime.now(timezone.utc)

                # Update status history (keep last 20 entries)
                # Initialize status_history if it doesn't exist or is None
                if not hasattr(waha_instance, "status_history") or waha_instance.status_history is None:
                    waha_instance.status_history = []

                # Add current status to history
                if status_history:
                    # Convert timestamp to datetime for each status
                    for status_item in status_history:
                        if isinstance(status_item.get("timestamp"), (int, float)):
                            status_item["timestamp"] = datetime.fromtimestamp(
                                status_item["timestamp"] / 1000, tz=timezone.utc
                            )
                    waha_instance.status_history.extend(status_history)
                else:
                    waha_instance.status_history.append(
                        {"status": status, "timestamp": datetime.now(timezone.utc)}
                    )

                # Keep only last 20 status changes
                waha_instance.status_history = waha_instance.status_history[-20:]

                # Update the account in the tenant's list
                tenant.waha_accounts = [
                    (waha_instance if acct.phone_number == waha_instance.phone_number else acct)
                    for acct in tenant.waha_accounts
                ]
                tenant.save()

                # Schedule a deferred check: if still not WORKING after 10 min, notify once.
                # A Redis lock prevents duplicate scheduling; a second key prevents re-notifying
                # while the session is still down (both keys are cleared on WORKING).
                _schedule_disconnect_check_if_needed(tenant_name, session_name, status)
        return True
    return False


_NON_WORKING_STATUSES = {"STARTING", "SCAN_QR_CODE", "FAILED"}


def _schedule_disconnect_check_if_needed(tenant_name: str, session_name: str, status: str):
    """On a non-working session status, schedule check_waha_session_task in 10 minutes (Redis lock against
    duplicates); on WORKING clear the notification flag.
    """
    phone = session_name.replace("+", "")
    pending_key = f"waha_pending_check:{tenant_name}:{phone}"
    notif_key = f"waha_notif_sent:{tenant_name}:{phone}"
    try:
        from background_tasks import check_waha_session_task, redis_client

        if status in _NON_WORKING_STATUSES:
            # Only schedule if no notification was already sent for this disconnect window
            # and no check is already pending.
            if not redis_client.exists(notif_key) and redis_client.set(pending_key, "1", nx=True, ex=780):
                check_waha_session_task.apply_async(args=[tenant_name, phone], countdown=600)
        elif status == "WORKING":
            # Session recovered — reset so the next disconnect triggers a fresh notification.
            redis_client.delete(notif_key)
    except Exception as e:
        logger.error(f"[waha] Failed to schedule disconnect check for {tenant_name}/{phone}: {e}")


def _extract_push_name(payload: dict, default: str = "") -> str:
    """Return the sender's display / push name across WAHA engines.

    WEBJS exposes it as ``_data.notifyName``; NOWEB/GOWS use ``_data.pushName``
    (a top-level ``notifyName`` may also be present). Falls back to *default*.
    """
    _data = payload.get("_data", {}) or {}
    return _data.get("notifyName") or _data.get("pushName") or payload.get("notifyName") or default


def _pn_from_payload_key(data: dict, lid_string: str) -> str:
    """Map a @lid to its real phone number (PN) using the webhook payload only.

    The NOWEB engine delivers the PN alongside the LID inside ``_data.key`` as an
    "Alt" field (``remoteJid`` / ``remoteJidAlt``), so the mapping is available
    without any extra API call. WEBJS chats arrive as ``@c.us`` and never reach
    this path. Returns the cleaned phone number when the given LID is the chat's
    LID, otherwise "".
    """
    key = (data.get("payload", {}) or {}).get("_data", {}).get("key", {}) or {}
    lid_val = key.get("remoteJid", "") or ""
    pn_val = key.get("remoteJidAlt", "") or ""
    if pn_val and "@lid" in lid_val and lid_val.split("@")[0] == lid_string.split("@")[0]:
        return pn_val.replace("@s.whatsapp.net", "").replace("@c.us", "")
    return ""


def _resolve_lid_to_phone_number(data: dict, tenant_name: str, lid_string: str) -> str:
    """
    Resolve a LID (Linked Device ID) to an actual phone number.
    LID format: 55285456584813@lid

    Resolution order:
      1. NOWEB payload (_data.key.*Alt) — no API call, no NOWEB Store dependency.
      2. WAHA /lids/{lid} endpoint.
      3. Fallback: the LID itself (so we never crash).

    Returns the phone number if found, otherwise returns the LID without the @lid suffix.
    """
    lid_id = lid_string.split("@")[0]

    # 1. Prefer the PN delivered inline in the NOWEB payload.
    pn_from_payload = _pn_from_payload_key(data, lid_string)
    if pn_from_payload:
        return pn_from_payload

    try:
        session_name = data.get("session", "").replace("+", "")
        if session_name:
            service = WahaService(tenant_name, session_name)
            lid_info = service.get_phone_number_by_lid(lid_string)
            # get_phone_number_by_lid returns {"pn": "phone_number", ...}
            resolved_phone = lid_info.get("pn", "")
            if resolved_phone:
                # Clean the phone number (remove @c.us if present)
                return resolved_phone.replace("@c.us", "").replace("@s.whatsapp.net", "")
    except Exception as e:
        logger.error(f"Error resolving LID {lid_string} to phone number: {e}")

    # Fallback: return the LID itself (not ideal but better than nothing)
    return lid_id


def _resolve_recipient_name(data: dict, tenant_name: str, chat_id: str) -> Optional[str]:
    """Resolve the chat recipient's display name for fromMe messages.

    For outgoing (fromMe) messages, the webhook's ``notifyName`` is the
    *sender's* push name (the business's own account), NOT the person
    we are writing to. To get the real recipient name ,
    query WAHA's chat info, which returns the contact/chat name.

    Returns the resolved name, or None if it could not be determined.
    """
    try:
        session_name = data.get("session", "").replace("+", "")
        if not session_name or not chat_id:
            return None
        service = WahaService(tenant_name, session_name)
        chat_info = service.get_chat_info(chat_id)
        name = chat_info.get("name")
        if name:
            return name
    except Exception as e:
        logger.error(f"Error resolving recipient name for {chat_id}: {e}")
    return None


def create_or_get_customer(data: dict, tenant_name: str, from_me: bool = False) -> CustomerDocument:
    """Create or retrieve the customer of a private WAHA chat.

    Inbound: the customer is the sender (``payload.from``). Outgoing (``fromMe``): the
    customer is the recipient. On WEBJS that is ``payload.to``; the NOWEB engine omits
    ``to`` and carries the chat partner in ``payload.from`` (as @c.us or @lid), so fall
    back to it. Otherwise the customer resolves to an empty phone_number, every manual
    reply collapses into one shared conversation and the human-takeover guard (manager
    messages correlated by phone_number + conversation_id) never fires.
    """
    payload = data.get("payload", {})
    if from_me:
        chat_field = payload.get("to", "") or payload.get("from", "")
    else:
        chat_field = payload.get("from", "")
    if "@lid" in chat_field:
        phone_number = _resolve_lid_to_phone_number(data, tenant_name, chat_field)
    else:
        phone_number = chat_field.split("@")[0]

    # For fromMe messages notifyName/pushName is OUR push name (the sender), never the recipient's
    sender_name = phone_number if from_me else _extract_push_name(payload, phone_number)

    customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    if customer:
        # Fill in the name once we know it (a customer created from an outgoing chat may still carry the number)
        if sender_name and sender_name != customer.name and customer.name in [None, "", customer.phone_number]:
            customer.name = sender_name
            customer.save()
        return customer

    # New customer reached through an outgoing chat: ask WAHA for the chat name
    if from_me:
        recipient_name = _resolve_recipient_name(data, tenant_name, payload.get("to", "") or phone_number)
        if recipient_name:
            sender_name = recipient_name
    customer = CustomerDocument(
        tenant_name=tenant_name,
        phone_number=phone_number,
        name=sender_name,
        platform="waha",
        created_date=datetime.now(timezone.utc).replace(microsecond=0),
        reply_mode="AI",
    )
    try:
        customer.save()
        customer.reload()
    except NotUniqueError:
        customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    return customer


def prepare_body_message(payload: dict) -> str:
    """Extract the message text from the WAHA payload."""
    body_message = payload.get("body", "")

    # WAHA NOWEB puts the jpegThumbnail base64 in `payload.body` for media messages
    # that have no caption — feeding that to the AI makes it echo the blob. When the
    # NOWEB media node is present, its caption is authoritative (empty when none).
    # WEBJS payloads have no `_data.message.*Message` node, so they keep payload.body.
    if payload.get("hasMedia"):
        message_node = (payload.get("_data", {}) or {}).get("message", {}) or {}
        doc_with_caption = message_node.get("documentWithCaptionMessage")
        if isinstance(doc_with_caption, dict):
            message_node = doc_with_caption.get("message", {}) or {}
        for media_key in ("imageMessage", "videoMessage", "documentMessage"):
            media_node = message_node.get(media_key)
            if isinstance(media_node, dict):
                body_message = media_node.get("caption", "") or ""
                break

    return body_message


def get_message_media_url(data, tenant_name):
    """Extract media URL and message type from WAHA Pro data structure"""
    payload = data.get("payload", {})

    # Initialize default values
    gcp_media_url = None
    message_type = "text"  # Default to text message

    # Check if the message has media
    has_media = payload.get("hasMedia", False)

    if has_media:
        # WAHA puts the downloadable link + mimetype on `payload.media`
        # ({"url": ..., "mimetype": "image/jpeg"}). NOWEB sends media=null when the
        # file isn't ready, so guard against None. Fall back to _data.mimetype for
        # engines/payloads that only expose it there.
        media = payload.get("media") or {}
        media_data = payload.get("_data", {}) or {}
        media_url = media.get("url", "")

        # Determine media type
        mime_type = media.get("mimetype") or media_data.get("mimetype", "")
        if "image" in mime_type:
            message_type = "image"
        elif "video" in mime_type:
            message_type = "video"
        elif "audio" in mime_type:
            message_type = "audio"
        elif "application" in mime_type or "document" in mime_type:
            message_type = "document"
        else:
            message_type = "media"

        # Save media to GCP bucket - MUST succeed or return None
        if media_url:
            max_retries = 3
            retry_count = 0
            last_error = None

            while retry_count < max_retries:
                try:
                    gcp_media_url = save_file_url_in_bucket(tenant_name, media_url, mime_type)
                    if gcp_media_url:
                        # Successfully saved to GCP
                        break
                except Exception as e:
                    last_error = e
                    retry_count += 1
                    logger.error(f"Error saving media to GCP (attempt {retry_count}/{max_retries}): {e}")

                    if retry_count < max_retries:
                        import time

                        time.sleep(1)  # Wait 1 second before retry

            if not gcp_media_url:
                # All retries failed - log error but don't save media
                error_msg = f"Failed to save media to GCP after {max_retries} attempts"
                if last_error:
                    error_msg += f": {last_error}"
                logger.info(error_msg)
                # Return None instead of falling back to original URL
                # This ensures we don't save external URLs that might expire
                gcp_media_url = None

    return gcp_media_url, message_type
