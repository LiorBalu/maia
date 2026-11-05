"""WAHA (WhatsApp) webhook handler: session status events and private-chat messages (text, media, voice notes, quoted replies), saving and dispatch."""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from mongoengine.errors import NotUniqueError

from maia.inbound.save.common import (
    SaveResponse,
    ensure_tenant_chat_settings,
    extract_text_from_attachment,
    download_and_transcribe_voice_note,
    resolve_human_like_delay_seconds,
    fill_missing_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.services.waha import WahaService
from maia.utils.google_cloud_storage.utils.save_file_url_in_bucket import upload_media_from_url_to_bucket

logger = logging.getLogger(__name__)

# WhatsApp group chat ids end with this JID suffix. The bot only serves private chats.
GROUP_CHAT_ID_SUFFIX = "@g.us"


def _is_private_chat_message(webhook_event: dict) -> bool:
    """True for a 'message' / 'message.any' event of a private chat; False for any other event, a group chat (by
    payload.from or payload.to) and a WhatsApp Status broadcast."""
    event_type = webhook_event.get("event")
    message_payload = webhook_event.get("payload", {})
    # "message.ack" (delivery receipts), reactions, polls and calls are not messages
    if event_type not in ["message", "message.any"]:
        return False

    sender_address = message_payload.get("from", "")
    # The bot does not take part in group chats and ignores WhatsApp Status broadcasts
    if (
        GROUP_CHAT_ID_SUFFIX in sender_address
        or GROUP_CHAT_ID_SUFFIX in message_payload.get("to", "")
        or sender_address.startswith("status@")
    ):
        return False
    return True


def _extract_message_ids(message_payload: dict) -> tuple[Optional[str], str]:
    """The two ids WAHA gives a message: the raw engine id (WEBJS exposes it at _data.id.id; NOWEB/GOWS at
    _data.key.id) and the serialized payload.id; None / '' when absent."""
    raw_message_id = (
        message_payload.get("_data", {}).get("id", {}).get("id")
        or message_payload.get("_data", {}).get("key", {}).get("id")
    )
    serialized_message_id = message_payload.get("id", "")
    return raw_message_id, serialized_message_id


def _is_duplicate_message(message_payload: dict, tenant_name: str) -> bool:
    """True when the tenant already has a message with one of the ids of _extract_message_ids (as its sid, then as
    its meta_data.fake_sid), or when the lookup raises NotUniqueError."""
    raw_message_id, serialized_message_id = _extract_message_ids(message_payload)
    try:
        if raw_message_id or serialized_message_id:
            candidate_sids = [candidate_sid for candidate_sid in [raw_message_id, serialized_message_id] if candidate_sid]
            duplicate_message_doc = MessageDocument.objects(
                # search 2 options because WAHA sometimes sends different IDs
                sid__in=candidate_sids,
                tenant_name=tenant_name,
            ).first()
            if duplicate_message_doc:
                return True
            # Double check in meta_data for serialized_message_id
            duplicate_message_doc = MessageDocument.objects(
                meta_data__fake_sid__in=candidate_sids, tenant_name=tenant_name
            ).first()
            if duplicate_message_doc:
                return True
    except NotUniqueError:
        return True
    return False


def _verify_tenant_is_active(tenant_name: str) -> None:
    """Raise ValueError when the tenant does not exist or is inactive."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        raise ValueError(f"Tenant {tenant_name} not found")
    if tenant_doc.status == "inactive":
        raise ValueError(f"Tenant {tenant_name} is inactive")


def _prepend_quoted_text(message_payload: dict, message_text: str) -> str:
    """For a quoted reply (_data.quotedStanzaID), the text prefixed with '[Replying to: "<quoted text>"]' on its own
    line; otherwise the text unchanged."""
    quoted_message_sid = message_payload.get("_data", {}).get("quotedStanzaID")
    if not quoted_message_sid:
        return message_text
    quoted_message_body = message_payload.get("_data", {}).get("quotedMsg", {}).get("body", "")
    return f'[Replying to: "{quoted_message_body}"]\n{message_text}'


def _resolve_connected_number(webhook_event: dict, is_outgoing: bool, tenant_name: str) -> str:
    """Our connected account number: payload.to (payload.from for an outgoing message), a '@lid' resolved through
    _resolve_phone_number_from_lid, otherwise without '@c.us'; the session name without '+' when that is empty."""
    # `connected_number` is our connected account number: the send path resolves the WAHA
    # connection with it (WahaService(tenant_name, to)). NOWEB often omits
    # payload.to; the WAHA session name *is* that number, so fall back to it.
    message_payload = webhook_event.get("payload", {})
    own_account_address = message_payload.get("to", "") if not is_outgoing else message_payload.get("from", "")
    if "@lid" in own_account_address:
        connected_number = _resolve_phone_number_from_lid(webhook_event, tenant_name, own_account_address)
    else:
        connected_number = own_account_address.replace("@c.us", "")
    if not connected_number:
        connected_number = webhook_event.get("session", "").replace("+", "")
    return connected_number


def _resolve_media_mime_type(message_payload: dict, message_type: str) -> str:
    """The attachment's _data.mimetype, defaulting to image/jpeg for an image and application/pdf for a document."""
    attachment_mime_type = message_payload.get("_data", {}).get("mimetype", "")
    if not attachment_mime_type and message_type == "image":
        attachment_mime_type = "image/jpeg"
    elif not attachment_mime_type and message_type == "document":
        attachment_mime_type = "application/pdf"
    return attachment_mime_type


def _enrich_message_text_from_media(
    message_text: str,
    media_url: Optional[str],
    message_type: str,
    attachment_mime_type: str,
    chat_settings_doc,
    tenant_name: str,
) -> str:
    """The message text with what its media adds: a voice-note transcript replaces it, then the text extracted from
    an image or a document is appended after a blank line."""
    if audio_transcript := download_and_transcribe_voice_note(media_url, message_type, tenant_name):
        message_text = audio_transcript

    # Extract text from media (images, PDFs, documents)
    if media_url and message_type in ["image", "document"]:
        if attachment_text := extract_text_from_attachment(media_url, message_type, attachment_mime_type, chat_settings_doc):
            message_text += f"\n\n{attachment_text}"
    return message_text


def _resolve_message_role_and_delay(
    message_payload: dict, is_outgoing: bool, customer_doc: CustomerDocument, reply_delay: int
) -> Optional[tuple[str, int]]:
    """The role the message is stored with and the reply delay: our own messages are 'assistant' (sent through the
    WAHA API) or 'manager' (from the phone) with no delay; the customer's are 'user', with no delay in 'agent' reply
    mode; None for a blocked customer (nothing is saved)."""
    # Determine the role based on who sent the message
    if is_outgoing:
        # Sent by us: from the phone -> 'manager', through the WAHA API (the bot) -> 'assistant'
        message_role = "assistant" if message_payload.get("source") == "api" else "manager"
        # Don't apply delay for our own messages
        return message_role, 0
    # Handle the reply modes
    if customer_doc.reply_mode == "blocked":
        return None
    if customer_doc.reply_mode == "agent":
        return "user", 0
    return "user", reply_delay


def _build_waha_message_meta_data(webhook_event: dict, message_payload: dict, is_outgoing: bool) -> dict:
    """The persisted meta_data of a WAHA message: session, the quoted message id, from_me, ack and timestamp."""
    return {
        "session": webhook_event.get("session", ""),
        "replit_message_sid": message_payload.get("_data", {}).get("quotedStanzaID"),
        "from_me": is_outgoing,
        "ack": message_payload.get("ack"),
        "timestamp": message_payload.get("timestamp"),
    }


async def save_waha_message(webhook_event: dict, tenant_name: Optional[str] = None) -> SaveResponse:  # NOSONAR
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
    if _consume_waha_session_status_event(webhook_event, tenant_name):
        return SaveResponse()
    if not _is_private_chat_message(webhook_event):
        return SaveResponse()

    message_payload = webhook_event.get("payload", {})
    is_outgoing = message_payload.get("fromMe", False)

    # Early duplicate check - do this BEFORE any heavy processing to prevent race conditions.
    # WEBJS exposes the raw id at _data.id.id; NOWEB/GOWS at _data.key.id.
    # We store outgoing AI messages with that same raw id, so the fromMe echo
    # de-duplicates here instead of being re-saved as a spurious "manager" msg.
    if _is_duplicate_message(message_payload, tenant_name):
        return SaveResponse()

    _verify_tenant_is_active(tenant_name)
    chat_settings_doc = ensure_tenant_chat_settings(tenant_name)
    reply_delay = resolve_human_like_delay_seconds(chat_settings_doc)

    customer_doc = _ensure_waha_chat_customer(webhook_event, tenant_name, is_outgoing)
    message_text = _extract_message_text(message_payload)
    media_url, message_type = _upload_media_and_classify_type(webhook_event, tenant_name)

    # Quoted reply: prepend the quoted text so the model sees what the customer answers to
    message_text = _prepend_quoted_text(message_payload, message_text)
    connected_number = _resolve_connected_number(webhook_event, is_outgoing, tenant_name)
    attachment_mime_type = _resolve_media_mime_type(message_payload, message_type)
    message_text = _enrich_message_text_from_media(
        message_text, media_url, message_type, attachment_mime_type, chat_settings_doc, tenant_name
    )

    message_role_and_delay = _resolve_message_role_and_delay(message_payload, is_outgoing, customer_doc, reply_delay)
    if message_role_and_delay is None:
        return SaveResponse()
    message_role, reply_delay = message_role_and_delay

    # For outgoing (fromMe) chats notifyName/pushName is OUR push name, not the
    # recipient's, so the message and the conversation are named after the customer.
    customer_display_name = (
        customer_doc.name if is_outgoing else _extract_sender_display_name(message_payload, customer_doc.phone_number)
    )

    if not message_text and not media_url:
        # No message to save
        return SaveResponse()
    raw_message_id, serialized_message_id = _extract_message_ids(message_payload)
    message_doc = MessageDocument(
        tenant_name=tenant_name,
        platform="waha",
        phone_number=customer_doc.phone_number,
        sender_id=str(customer_doc.id),
        profile_name=customer_display_name,
        message=message_text,
        role=message_role,
        to=connected_number,
        msg_type=message_type,
        media_url=media_url,
        sid=raw_message_id or serialized_message_id or str(uuid.uuid4()),
        message_sids=[],
        date=datetime.now(timezone.utc),
        meta_data=_build_waha_message_meta_data(webhook_event, message_payload, is_outgoing),
    )
    try:
        message_doc.save()
    except NotUniqueError:
        return SaveResponse()
    fill_missing_conversation_profile_name(
        tenant_name, customer_doc.phone_number, customer_display_name, message_doc.conversation_id
    )

    # If message is from us (fromMe=true), don't trigger AI response
    if is_outgoing:
        return SaveResponse()
    return SaveResponse(
        tenant_name=tenant_name,
        sender_id=customer_doc.phone_number,
        reply_delay_seconds=reply_delay,
        triggering_message_id=str(message_doc.id),
        conversation_id=message_doc.conversation_id,
    )


# Status emoji mapping for session status notifications
SESSION_STATUS_EMOJIS = {"WORKING": "🟢", "STOPPED": "🔴", "STARTING": "🟡", "SCAN_QR_CODE": "📱", "FAILED": "🚨"}


# Hebrew status labels
SESSION_STATUS_LABELS_HE = {
    "WORKING": "מחובר",
    "STOPPED": "מנותק",
    "STARTING": "מתחיל",
    "SCAN_QR_CODE": "סריקת QR",
    "FAILED": "נכשל",
}


SESSION_STATUS_LABELS_EN = {
    "WORKING": "Connected",
    "STOPPED": "Stopped",
    "STARTING": "Starting",
    "SCAN_QR_CODE": "Scan QR Code",
    "FAILED": "Failed",
}


def _render_session_status_email(
    tenant_doc, tenant_name: str, tenant_phone_number: str, session_status: str
) -> tuple[str, str]:
    """Subject and HTML body of the session status mail: Hebrew (right to left) when the tenant's default language
    is Hebrew or unset, English otherwise."""
    language = getattr(tenant_doc, "default_language", "he") or "he"
    status_emoji = SESSION_STATUS_EMOJIS.get(session_status, "❓")
    status_label_he = SESSION_STATUS_LABELS_HE.get(session_status, session_status)
    status_label_en = SESSION_STATUS_LABELS_EN.get(session_status, session_status)
    status_label = status_label_he if language == "he" else status_label_en
    tenant_display_name = tenant_name.replace("-", " ").replace("_", " ").title()
    if language == "he":
        subject = f"{status_emoji} עדכון סטטוס חיבור - {tenant_display_name}"
        html_body = f"""
            <div dir="rtl" style="font-family: Arial, sans-serif; padding: 20px;">
                <h2>{status_emoji} עדכון סטטוס חיבור</h2>
                <p><strong>שם העסק:</strong> {tenant_display_name}</p>
                <p><strong>מספר טלפון:</strong> {tenant_phone_number}</p>
                <p><strong>סטטוס:</strong> {status_emoji} {status_label}</p>
            </div>
            """
    else:
        subject = f"{status_emoji} Connection Status Update - {tenant_display_name}"
        html_body = f"""
            <div style="font-family: Arial, sans-serif; padding: 20px;">
                <h2>{status_emoji} Connection Status Update</h2>
                <p><strong>Tenant:</strong> {tenant_display_name}</p>
                <p><strong>Phone Number:</strong> {tenant_phone_number}</p>
                <p><strong>Status:</strong> {status_emoji} {status_label}</p>
            </div>
            """
    return subject, html_body


def notify_tenant_users_of_session_status(tenant_doc, tenant_name: str, tenant_phone_number: str, session_status: str):
    """Email every user with access to the tenant about a session status change, from the tenant's connected Gmail
    account (nothing is sent when the tenant has none).
    """
    try:
        from maia.models.users import UserDocument
        from maia.services.email_platform import send_email_from_tenant_gmail

        # Find all users that have permissions for this tenant
        dashboard_users = UserDocument.objects(**{f"permissions__{tenant_name}__exists": True})
        if not dashboard_users:
            return

        subject, html_body = _render_session_status_email(tenant_doc, tenant_name, tenant_phone_number, session_status)

        for dashboard_user in dashboard_users:
            if not dashboard_user.email:
                continue
            try:
                send_email_from_tenant_gmail(
                    tenant_name, recipient_email=dashboard_user.email, subject_line=subject, message_html=html_body
                )
            except Exception as error:
                logger.error(f"Error sending email to {dashboard_user.email} for {tenant_name}: {error}")

    except Exception as error:
        logger.error(f"Error sending session status notification for {tenant_name}: {error}")


def _record_session_status_on_account(tenant_doc: TenantDocument, waha_session_name: str, session_status: str) -> bool:
    """Store the status on the tenant's WAHA account of that session (is_connected, session_status,
    last_status_update) and save the tenant; False, with nothing saved, when no account has the session's number."""
    waha_account = tenant_doc.find_waha_account_by_phone_number(waha_session_name.replace("+", ""))
    if not waha_account:
        return False
    # WAHA statuses: STOPPED, STARTING, SCAN_QR_CODE, WORKING, FAILED
    waha_account.is_connected = session_status == "WORKING"
    waha_account.session_status = session_status
    waha_account.last_status_update = datetime.now(timezone.utc)

    # Update the account in the tenant's list
    tenant_doc.waha_accounts = [
        (waha_account if stored_account.phone_number == waha_account.phone_number else stored_account)
        for stored_account in tenant_doc.waha_accounts
    ]
    tenant_doc.save()
    return True


def _consume_waha_session_status_event(webhook_event, tenant_name):
    """Check if the message is a connection status message"""
    # WAHA Pro sends session.status events
    event_type = webhook_event.get("event")
    if event_type in ["session.status", "session.created"]:
        # Update tenant connection status
        tenant_doc = TenantDocument.objects(name=tenant_name).first()
        if tenant_doc:
            session_payload = webhook_event.get("payload", {})
            waha_session_name = session_payload.get("name", "")
            session_status = session_payload.get("status", "")

            if _record_session_status_on_account(tenant_doc, waha_session_name, session_status):
                # Schedule a deferred check: if still not WORKING after 10 min, notify once.
                # A Redis lock prevents duplicate scheduling; a second key prevents re-notifying
                # while the session is still down (both keys are cleared on WORKING).
                _schedule_disconnect_check_or_clear_notified_flag(tenant_name, waha_session_name, session_status)
        return True
    return False


_DISCONNECT_CHECK_STATUSES = {"STARTING", "SCAN_QR_CODE", "FAILED"}


def _schedule_disconnect_check_or_clear_notified_flag(tenant_name: str, waha_session_name: str, session_status: str):
    """On a non-working session status, schedule notify_if_waha_session_still_disconnected_task in 10 minutes (Redis lock against
    duplicates); on WORKING clear the notification flag.
    """
    tenant_phone_number = waha_session_name.replace("+", "")
    pending_check_key = f"waha_pending_check:{tenant_name}:{tenant_phone_number}"
    notified_flag_key = f"waha_notif_sent:{tenant_name}:{tenant_phone_number}"
    try:
        from background_tasks import notify_if_waha_session_still_disconnected_task, sync_redis_client

        if session_status in _DISCONNECT_CHECK_STATUSES:
            # Only schedule if no notification was already sent for this disconnect window
            # and no check is already pending.
            if not sync_redis_client.exists(notified_flag_key) and sync_redis_client.set(
                pending_check_key, "1", nx=True, ex=780
            ):
                notify_if_waha_session_still_disconnected_task.apply_async(
                    args=[tenant_name, tenant_phone_number], countdown=600
                )
        elif session_status == "WORKING":
            # Session recovered — reset so the next disconnect triggers a fresh notification.
            sync_redis_client.delete(notified_flag_key)
    except Exception as error:
        logger.error(f"[waha] Failed to schedule disconnect check for {tenant_name}/{tenant_phone_number}: {error}")


def _extract_sender_display_name(message_payload: dict, fallback_name: str = "") -> str:
    """Return the sender's display / push name across WAHA engines.

    WEBJS exposes it as ``_data.notifyName``; NOWEB/GOWS use ``_data.pushName``
    (a top-level ``notifyName`` may also be present). Falls back to *fallback_name*.
    """
    engine_data = message_payload.get("_data", {}) or {}
    return (
        engine_data.get("notifyName")
        or engine_data.get("pushName")
        or message_payload.get("notifyName")
        or fallback_name
    )


def _find_phone_number_in_payload_key(webhook_event: dict, lid_chat_id: str) -> str:
    """Map a @lid to its real phone number (PN) using the webhook payload only.

    The NOWEB engine delivers the PN alongside the LID inside ``_data.key`` as an
    "Alt" field (``remoteJid`` / ``remoteJidAlt``), so the mapping is available
    without any extra API call. WEBJS chats arrive as ``@c.us`` and never reach
    this path. Returns the cleaned phone number when the given LID is the chat's
    LID, otherwise "".
    """
    message_key = (webhook_event.get("payload", {}) or {}).get("_data", {}).get("key", {}) or {}
    remote_lid_address = message_key.get("remoteJid", "") or ""
    remote_phone_address = message_key.get("remoteJidAlt", "") or ""
    if (
        remote_phone_address
        and "@lid" in remote_lid_address
        and remote_lid_address.split("@")[0] == lid_chat_id.split("@")[0]
    ):
        return remote_phone_address.replace("@s.whatsapp.net", "").replace("@c.us", "")
    return ""


def _fetch_phone_number_by_lid_from_waha(webhook_event: dict, tenant_name: str, lid_chat_id: str) -> Optional[str]:
    """The LID's phone number from WAHA's /lids/{lid} endpoint, through the event's session, without @c.us /
    @s.whatsapp.net; None when the event has no session, WAHA returned no 'pn' or the call failed (logged)."""
    try:
        waha_session_name = webhook_event.get("session", "").replace("+", "")
        if waha_session_name:
            waha_service = WahaService(tenant_name, waha_session_name)
            lid_lookup = waha_service.fetch_phone_number_for_lid(lid_chat_id)
            # fetch_phone_number_for_lid returns {"pn": "phone_number", ...}
            phone_number_from_api = lid_lookup.get("pn", "")
            if phone_number_from_api:
                # Clean the phone number (remove @c.us if present)
                return phone_number_from_api.replace("@c.us", "").replace("@s.whatsapp.net", "")
    except Exception as error:
        logger.error(f"Error resolving LID {lid_chat_id} to phone number: {error}")
    return None


def _resolve_phone_number_from_lid(webhook_event: dict, tenant_name: str, lid_chat_id: str) -> str:
    """
    Resolve a LID (Linked Device ID) to an actual phone number.
    LID format: 55285456584813@lid

    Resolution order:
      1. NOWEB payload (_data.key.*Alt) — no API call, no NOWEB Store dependency.
      2. WAHA /lids/{lid} endpoint.
      3. Fallback: the LID itself (so we never crash).

    Returns the phone number if found, otherwise returns the LID without the @lid suffix.
    """
    lid_digits = lid_chat_id.split("@")[0]

    # 1. Prefer the PN delivered inline in the NOWEB payload.
    phone_number_from_payload = _find_phone_number_in_payload_key(webhook_event, lid_chat_id)
    if phone_number_from_payload:
        return phone_number_from_payload

    # 2. Ask WAHA (WahaService is constructed only now, after the payload had no PN).
    phone_number_from_waha = _fetch_phone_number_by_lid_from_waha(webhook_event, tenant_name, lid_chat_id)
    if phone_number_from_waha is not None:
        return phone_number_from_waha

    # Fallback: return the LID itself (not ideal but better than nothing)
    return lid_digits


def _fetch_recipient_name_from_waha(webhook_event: dict, tenant_name: str, recipient_phone_or_chat_id: str) -> Optional[str]:
    """Resolve the chat recipient's display name for fromMe messages.

    For outgoing (fromMe) messages, the webhook's ``notifyName`` is the
    *sender's* push name (the business's own account), NOT the person
    we are writing to. To get the real recipient name ,
    query WAHA's chat info, which returns the contact/chat name.

    Returns the resolved name, or None if it could not be determined.
    """
    try:
        waha_session_name = webhook_event.get("session", "").replace("+", "")
        if not waha_session_name or not recipient_phone_or_chat_id:
            return None
        waha_service = WahaService(tenant_name, waha_session_name)
        chat_overview = waha_service.fetch_chat_overview(recipient_phone_or_chat_id)
        chat_name = chat_overview.get("name")
        if chat_name:
            return chat_name
    except Exception as error:
        logger.error(f"Error resolving recipient name for {recipient_phone_or_chat_id}: {error}")
    return None


def _resolve_chat_partner_phone_number(webhook_event: dict, tenant_name: str, is_outgoing: bool) -> str:
    """The chat partner's phone number from its address (payload.from inbound; payload.to, else payload.from,
    outgoing): a '@lid' through _resolve_phone_number_from_lid, otherwise the part before '@'."""
    message_payload = webhook_event.get("payload", {})
    if is_outgoing:
        chat_partner_address = message_payload.get("to", "") or message_payload.get("from", "")
    else:
        chat_partner_address = message_payload.get("from", "")
    if "@lid" in chat_partner_address:
        return _resolve_phone_number_from_lid(webhook_event, tenant_name, chat_partner_address)
    return chat_partner_address.split("@")[0]


def _fill_customer_name_if_unknown(customer_doc: CustomerDocument, customer_name: str) -> None:
    """Set and save the customer's name when a real name is known and the stored one is None, '' or the phone
    number."""
    # Fill in the name once we know it (a customer created from an outgoing chat may still carry the number)
    if (
        customer_name
        and customer_name != customer_doc.name
        and customer_doc.name in [None, "", customer_doc.phone_number]
    ):
        customer_doc.name = customer_name
        customer_doc.save()


def _create_waha_customer(tenant_name: str, phone_number: str, customer_name: str) -> CustomerDocument:
    """Create and save the customer of a WAHA chat (reply mode 'AI'); the stored one when it already exists
    (NotUniqueError)."""
    customer_doc = CustomerDocument(
        tenant_name=tenant_name,
        phone_number=phone_number,
        name=customer_name,
        platform="waha",
        created_date=datetime.now(timezone.utc).replace(microsecond=0),
        reply_mode="AI",
    )
    try:
        customer_doc.save()
        customer_doc.reload()
    except NotUniqueError:
        customer_doc = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    return customer_doc


def _ensure_waha_chat_customer(webhook_event: dict, tenant_name: str, is_outgoing: bool = False) -> CustomerDocument:
    """Create or retrieve the customer of a private WAHA chat.

    Inbound: the customer is the sender (``payload.from``). Outgoing (``fromMe``): the
    customer is the recipient. On WEBJS that is ``payload.to``; the NOWEB engine omits
    ``to`` and carries the chat partner in ``payload.from`` (as @c.us or @lid), so fall
    back to it. Otherwise the customer resolves to an empty phone_number, every manual
    reply collapses into one shared conversation and the human-takeover guard (manager
    messages correlated by phone_number + conversation_id) never fires.
    """
    message_payload = webhook_event.get("payload", {})
    customer_phone_number = _resolve_chat_partner_phone_number(webhook_event, tenant_name, is_outgoing)

    # For fromMe messages notifyName/pushName is OUR push name (the sender), never the recipient's
    customer_name = (
        customer_phone_number if is_outgoing else _extract_sender_display_name(message_payload, customer_phone_number)
    )

    customer_doc = CustomerDocument.objects(phone_number=customer_phone_number, tenant_name=tenant_name).first()
    if customer_doc:
        _fill_customer_name_if_unknown(customer_doc, customer_name)
        return customer_doc

    # New customer reached through an outgoing chat: ask WAHA for the chat name
    if is_outgoing:
        waha_chat_name = _fetch_recipient_name_from_waha(
            webhook_event, tenant_name, message_payload.get("to", "") or customer_phone_number
        )
        if waha_chat_name:
            customer_name = waha_chat_name
    return _create_waha_customer(tenant_name, customer_phone_number, customer_name)


def _extract_message_text(message_payload: dict) -> str:
    """Extract the message text from the WAHA payload."""
    message_text = message_payload.get("body", "")

    # WAHA NOWEB puts the jpegThumbnail base64 in `payload.body` for media messages
    # that have no caption — feeding that to the AI makes it echo the blob. When the
    # NOWEB media node is present, its caption is authoritative (empty when none).
    # WEBJS payloads have no `_data.message.*Message` node, so they keep payload.body.
    if message_payload.get("hasMedia"):
        message_node = (message_payload.get("_data", {}) or {}).get("message", {}) or {}
        caption_wrapper_node = message_node.get("documentWithCaptionMessage")
        if isinstance(caption_wrapper_node, dict):
            message_node = caption_wrapper_node.get("message", {}) or {}
        for media_message_key in ("imageMessage", "videoMessage", "documentMessage"):
            media_node = message_node.get(media_message_key)
            if isinstance(media_node, dict):
                message_text = media_node.get("caption", "") or ""
                break

    return message_text


def _classify_media_type(mime_type: str) -> str:
    """The message type of a media mimetype: image, video, audio, document (also any application/*) or media."""
    if "image" in mime_type:
        return "image"
    if "video" in mime_type:
        return "video"
    if "audio" in mime_type:
        return "audio"
    if "application" in mime_type or "document" in mime_type:
        return "document"
    return "media"


def _upload_media_to_bucket_with_retries(tenant_name: str, source_media_url: str, mime_type: str) -> Optional[str]:
    """Copy the media into the tenant's GCS bucket; a failed attempt is retried one second later, up to 3 failed
    attempts. Returns the bucket URL, or None when every attempt failed (never the external URL)."""
    bucket_media_url = None
    max_attempts = 3
    failed_attempt_count = 0
    last_upload_error = None

    while failed_attempt_count < max_attempts:
        try:
            bucket_media_url = upload_media_from_url_to_bucket(tenant_name, source_media_url, mime_type)
            if bucket_media_url:
                # Successfully saved to GCP
                break
        except Exception as error:
            last_upload_error = error
            failed_attempt_count += 1
            logger.error(f"Error saving media to GCP (attempt {failed_attempt_count}/{max_attempts}): {error}")

            if failed_attempt_count < max_attempts:
                time.sleep(1)  # Wait 1 second before retry

    if not bucket_media_url:
        # All retries failed - log error but don't save media
        failure_message = f"Failed to save media to GCP after {max_attempts} attempts"
        if last_upload_error:
            failure_message += f": {last_upload_error}"
        logger.info(failure_message)
        # Return None instead of falling back to original URL
        # This ensures we don't save external URLs that might expire
        bucket_media_url = None
    return bucket_media_url


def _upload_media_and_classify_type(webhook_event, tenant_name):
    """Extract media URL and message type from WAHA Pro data structure"""
    message_payload = webhook_event.get("payload", {})

    # Initialize default values
    bucket_media_url = None
    message_type = "text"  # Default to text message

    # Check if the message has media
    has_media = message_payload.get("hasMedia", False)

    if has_media:
        # WAHA puts the downloadable link + mimetype on `payload.media`
        # ({"url": ..., "mimetype": "image/jpeg"}). NOWEB sends media=null when the
        # file isn't ready, so guard against None. Fall back to _data.mimetype for
        # engines/payloads that only expose it there.
        payload_media = message_payload.get("media") or {}
        engine_data = message_payload.get("_data", {}) or {}
        source_media_url = payload_media.get("url", "")

        # Determine media type
        media_mime_type = payload_media.get("mimetype") or engine_data.get("mimetype", "")
        message_type = _classify_media_type(media_mime_type)

        # Save media to GCP bucket - MUST succeed or return None
        if source_media_url:
            bucket_media_url = _upload_media_to_bucket_with_retries(tenant_name, source_media_url, media_mime_type)

    return bucket_media_url, message_type
