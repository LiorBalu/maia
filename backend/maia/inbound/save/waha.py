"""WAHA (WhatsApp) webhook handler: session status events, message events (text, media, voice, mentions, groups), saving and dispatch."""

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
    update_customer_status_on_unsubscribe,
)
from maia.knowledge.chat_history import add_message_to_history
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.conversations import Tag
from maia.models.settings import ChatSettingsDocument
from maia.services.waha import WahaService
from maia.utils.conversation.get_or_create import get_or_create_online_conversation
from maia.utils.convert_media_url_to_base64 import convert_media_to_langchain_format
from maia.utils.google_cloud_storage.utils.save_file_url_in_bucket import save_file_url_in_bucket

logger = logging.getLogger(__name__)

# WhatsApp group chat ids always end with this JID suffix.
GROUP_JID_SUFFIX = "@g.us"


async def save_waha_input(data: dict, tenant_name: Optional[str] = None) -> SaveResponse:  # NOSONAR
    """Save incoming message from WAHA Pro webhook

    WAHA Pro webhook format:
    {
        "event": "message",
        "session": "session_name",
        "payload": {
            "id": "message_id",
            "timestamp": 1234567890,
            "from": "1234567890@c.us",
            "fromMe": false,
            "to": "0987654321@c.us",
            "body": "message text",
            "hasMedia": false,
            "ack": 1,
            "_data": {...}
        }
    }
    """

    is_connection_status_message = handle_connection_status_message(data, tenant_name)
    if is_connection_status_message:
        # If it's a connection status, we do not need to process it
        return SaveResponse()

    # Handle reaction messages
    is_reaction = handle_reaction_message(data, tenant_name)
    if is_reaction:
        # If it's a reaction, we don't need to process it further
        return SaveResponse()

    # Incoming WhatsApp calls are ignored (never processed as messages)
    if data.get("event") == "call.received":
        return SaveResponse()

    # Process "message", "message.any" and poll events
    # "message.ack" is only for delivery acknowledgments, skip it
    event = data.get("event")
    payload = data.get("payload", {})

    is_poll_vote = False
    if event == "poll.vote":
        is_poll_vote = True
    elif event == "engine.event" and payload.get("event") == "vote_update":
        is_poll_vote = True

    if event not in ["message", "message.any"] and not is_poll_vote:
        return SaveResponse()

    # Get payload data
    from_me = payload.get("fromMe", False)

    # Early duplicate check - do this BEFORE any heavy processing to prevent race conditions
    # This is the key fix for duplicate messages in groups
    if is_poll_vote:
        message_sid = data.get("id")
        fake_message_sid = ""
    else:
        # WEBJS exposes the raw id at _data.id.id; NOWEB/GOWS at _data.key.id.
        # We store outgoing AI messages with that same raw id, so the fromMe echo
        # de-duplicates here instead of being re-saved as a spurious "manager" msg.
        message_sid = payload.get("_data", {}).get("id", {}).get("id") or payload.get("_data", {}).get("key", {}).get(
            "id"
        )
        fake_message_sid = payload.get("id", "")
    try:
        if message_sid or fake_message_sid:
            sid__in = [sid for sid in [message_sid, fake_message_sid] if sid]
            existing_msg = MessageDocument.objects(
                # search 2 options because WAHA Pro sometimes sends different IDs
                sid__in=sid__in,
                tenant_name=tenant_name,
            ).first()
            if existing_msg:
                return SaveResponse()
            else:
                # Double check in meta_data for fake_message_sid
                existing_msg = MessageDocument.objects(meta_data__fake_sid__in=sid__in, tenant_name=tenant_name).first()
                if existing_msg:
                    return SaveResponse()
    except NotUniqueError:
        return SaveResponse()

   
    # Create message document
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise ValueError(f"Tenant {tenant_name} not found")
    if tenant.status == "inactive":
        raise ValueError(f"Tenant {tenant_name} is inactive")
    chat_settings = get_or_create_chat_settings(tenant_name)
    delay = retrieve_human_delay_setting(chat_settings)

    customer, _ = create_or_get_customer(data, tenant_name, chat_settings, from_me)
    body_message = prepare_body_message(data, tenant_name)
    media_url, message_type = get_message_media_url(data, tenant_name)

    # Get reply info if available
    replit_message_sid = None
    base_message = None
    if not is_poll_vote:
        replit_message_sid = payload.get("_data", {}).get("quotedStanzaID")
        if replit_message_sid:
            base_message = body_message
            quoted_body = payload.get("_data", {}).get("quotedMsg", {}).get("body", "")
            body_message = f'[Replying to: "{quoted_body}"]\n{body_message}'
    else:
        parent_message = payload.get("data", {}).get("parentMessage", {})
        pool_name = parent_message.get("_data", {}).get("pollName", "Poll")
        base_message = body_message
        body_message = f"*Poll Vote Received for '{pool_name}':* {body_message}"
    # Extract 'to' field and resolve LID if needed
    if is_poll_vote:
        # For poll votes, 'to' is in vote.to or poll.to
        to_field = payload.get("vote", {}).get("to", "") or payload.get("poll", {}).get("to", "")
    else:
        to_field = payload.get("to", "") if not from_me else payload.get("from", "")
    if "@lid" in to_field:
        # If 'to' contains a LID, resolve it to actual phone number
        to = _resolve_lid_to_phone_number(data, tenant_name, to_field)
    elif GROUP_JID_SUFFIX in to_field:
        # Group message: `to_field` is the GROUP JID, not our connected account.
        # The send path resolves the WAHA connection by account number, so
        # route it to the WAHA session; otherwise sending fails with
        # "WAHA connection '<group-jid>' not found" and the reply is lost.
        to = data.get("session", "").replace("+", "")
    else:
        # Otherwise just clean it
        to = to_field.replace("@c.us", "")

    # NOWEB often omits payload.to, leaving `to` empty. `to` is our connected
    # account number — it is what the send path uses to resolve the WAHA
    # connection (WahaService(tenant_name, to)). The WAHA session name *is*
    # that account number, so fall back to it; otherwise sending fails with
    # "WAHA connection '' not found" and the AI reply is never delivered.
    if not to:
        to = data.get("session", "").replace("+", "")

    if transcoded_audio := handle_audio_message(media_url, message_type, tenant_name):
        body_message = transcoded_audio

    # Extract text from media (images, PDFs, documents)
    if media_url and message_type in ["image", "document"]:
        # Get mime type from payload
        mime_type = payload.get("_data", {}).get("mimetype", "")
        if not mime_type and message_type == "image":
            mime_type = "image/jpeg"
        elif not mime_type and message_type == "document":
            mime_type = "application/pdf"

        if media_text := extract_text_from_media(media_url, message_type, mime_type, chat_settings):
            body_message += f"\n\n{media_text}"

    # Determine the role based on who sent the message
    if from_me:
        # Message sent by us from mobile app -> role is 'assistant'
        role = "assistant" if payload.get("source") == "api" else "manager"
        # Don't apply delay for our own messages
        delay = 0
    else:
        # Message received from other user -> role is 'user'
        role = "user"
        # Handle the reply modes
        if customer.reply_mode == "blocked":
            return SaveResponse()
        if customer.reply_mode == "agent":
            delay = 0

    # Get sender info
    sender_lid = ""
    if is_poll_vote:
        vote_data = payload.get("_data", {}) or payload.get("data", {})
        from_field = vote_data.get("voter", "")
        # Resolve LID if needed
        if "@lid" in from_field:
            sender_lid = from_field
            from_number = _resolve_lid_to_phone_number(data, tenant_name, from_field)
        else:
            from_number = from_field.split("@")[0]

        # Get voter's name if available
        voter_name = from_number
        # Try to get notify name from parent message or other sources
        parent_msg_data = vote_data.get("parentMessage", {}).get("_data", {})
        if parent_msg_data:
            # Try to find voter info in parent message
            voter_name = from_number  # Use phone as fallback

        sender_name = voter_name
    else:
        from_field = payload.get("from", "")

        # For group messages, get the actual sender from author/participant
        if GROUP_JID_SUFFIX in from_field:
            # Try author first, then participant
            actual_sender = payload.get("_data", {}).get("author") or payload.get("_data", {}).get("participant", "")
            if actual_sender:
                # Resolve LID if needed
                if "@lid" in actual_sender:
                    sender_lid = actual_sender
                    from_number = _resolve_lid_to_phone_number(data, tenant_name, actual_sender)
                else:
                    from_number = actual_sender.split("@")[0]
            else:
                # Fallback to from_field
                from_number = from_field.split("@")[0]
        else:
            # Resolve LID if needed for non-group messages
            if "@lid" in from_field:
                from_number = _resolve_lid_to_phone_number(data, tenant_name, from_field)
            else:
                from_number = from_field.split("@")[0]

        sender_name = _extract_push_name(payload, from_number)

    is_group_message = GROUP_JID_SUFFIX in (payload.get("from", "") or to_field or "")

    # For group messages, create/get an individual customer for the actual sender
    # so we can track participants and resolve LID mentions
    sender_customer = None
    if is_group_message and from_number and not from_me:
        sender_customer = _get_or_create_group_sender_customer(
            data=data, tenant_name=tenant_name, sender_phone=from_number, sender_lid=sender_lid, sender_name=sender_name
        )

    # Resolve LID mentions in body for group messages
    if is_group_message and not from_me:
        mentioned_jids = _extract_mentioned_ids(payload)
        if mentioned_jids:
            body_message = _resolve_mentions_in_body(
                data=data, tenant_name=tenant_name, body_message=body_message, mentioned_ids=mentioned_jids
            )

    # Use sender name for profile_name (group name is in metadata)
    profile_name_to_use = sender_name
    # For conversations, use group name for groups, sender name for individuals
    conversation_profile_name = customer.name if is_group_message else sender_name

    # For outgoing (fromMe) private chats, sender_name / notifyName is OUR
    # push name, not the recipient's. The chat & conversation must be named
    # after the recipient (resolved into customer.name in create_or_get_customer).
    if from_me and not is_group_message:
        profile_name_to_use = customer.name
        conversation_profile_name = customer.name

    if not body_message and not media_url:
        # No message to save
        return SaveResponse()
    msg = MessageDocument(
        tenant_name=tenant_name,
        platform="waha",
        phone_number=customer.phone_number,
        sender_id=str(customer.id),
        profile_name=profile_name_to_use,
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
            "is_group_message": is_group_message,
            "group_name": (customer.name if is_group_message else None),
            "original_sender_number": from_number,
            "original_sender_name": sender_name,
            "sender_lid": sender_lid or None,
            "sender_customer_id": str(sender_customer.id) if sender_customer else None,
            "replit_message_sid": replit_message_sid,
            "from_me": from_me,
            "ack": payload.get("ack"),
            "timestamp": payload.get("timestamp"),
            "base_message": base_message,
            "is_mentioned": False,
        },
    )
    try:
        msg.save()
    except NotUniqueError:
        return SaveResponse()
    update_conversation_profile_name(tenant_name, customer.phone_number, conversation_profile_name, msg.conversation_id)


    if is_poll_vote and base_message:
        conv = get_or_create_online_conversation(
            tenant_name=tenant_name,
            customer_id=customer.phone_number,
            platform="waha",
            profile_name=conversation_profile_name,
        )
        conv.tags.append(Tag(name=base_message, color="#FFCF33"))
        conv.save()

    # Only check for unsubscribe if message is from user (not from us)
    if not from_me and update_customer_status_on_unsubscribe(body_message, customer):
        return SaveResponse()

    # If message is from us (fromMe=true), don't trigger AI response
    # Also skip if it's a poll vote
    if from_me or is_poll_vote:
        return SaveResponse()
    # Skip WhatsApp Status/broadcast messages (status@c.us)
    from_field = payload.get("from", "")
    if from_field.startswith("status@"):
        return SaveResponse()
    # If customer is group and is customer.meta_data['force_send_to_group'] is true and reply only to mentions
    if (
        customer.meta_data.get("is_group")
        and customer.get_meta_data_field("force_send_to_group", False)
        and customer.get_meta_data_field("reply_only_to_mentions", True)
    ):
        session_name = data.get("session", "").replace("+", "")
        is_mentioned = False

        # Check if this is a reply to one of our messages
        if replit_message_sid:
            original_msg = MessageDocument.objects(sid=replit_message_sid, tenant_name=tenant_name).first()
            if original_msg and original_msg.role in ["assistant", "manager"]:
                is_mentioned = True

        # Check mentionedJidList (WhatsApp mentions)
        if not is_mentioned:
            mentioned_ids = _extract_mentioned_ids(payload)

            if mentioned_ids:
                service = None
                for mid in mentioned_ids:
                    mid_str = mid.get("_serialized", str(mid)) if isinstance(mid, dict) else str(mid)

                    # Clean the mentioned ID for comparison
                    clean_mid = (
                        mid_str.replace("@c.us", "").replace("@s.whatsapp.net", "").replace("@lid", "").replace("+", "")
                    )
                    clean_session = session_name.replace("+", "")
                    clean_to = to.replace("+", "") if to else ""

                    # Check if mentioned ID matches our session or 'to' number
                    if clean_session and clean_session in clean_mid:
                        is_mentioned = True
                        break
                    if clean_to and clean_to in clean_mid:
                        is_mentioned = True
                        break

                    # Handle LID (Linked Device ID)
                    if "@lid" in mid_str:
                        resolved_phone = None

                        # Try DB first (may already exist from _resolve_mentions_in_body)
                        lid_customer = CustomerDocument.objects(tenant_name=tenant_name, meta_data__lid=mid_str).first()
                        if lid_customer:
                            resolved_phone = lid_customer.phone_number.replace("+", "")
                        else:
                            # Fallback to API
                            if not service:
                                try:
                                    service = WahaService(tenant_name, session_name)
                                except Exception:
                                    continue

                            try:
                                lid_info = service.get_phone_number_by_lid(mid_str)
                                resolved_phone = (
                                    lid_info.get("pn", "")
                                    .replace("@c.us", "")
                                    .replace("@s.whatsapp.net", "")
                                    .replace("+", "")
                                )
                            except Exception:
                                pass

                        if resolved_phone:
                            if clean_session and clean_session in resolved_phone:
                                is_mentioned = True
                                break
                            if clean_to and clean_to in resolved_phone:
                                is_mentioned = True
                                break

        # Fallback: Check for @ mentions in message body (less reliable)
        if not is_mentioned and session_name:
            # Check various formats: @972123456789, @123456789, etc.
            clean_session = session_name.replace("+", "")
            if f"@{clean_session}" in body_message or f"@{session_name}" in body_message:
                is_mentioned = True

        # Update message metadata with is_mentioned value
        msg.meta_data["is_mentioned"] = is_mentioned
        msg.save()

        if not is_mentioned:
            logger.info("[GroupGate][%s][%s] → history only (no AI)", tenant_name, customer.phone_number)
            conv = get_or_create_online_conversation(
                tenant_name=tenant_name,
                customer_id=customer.phone_number,
                platform="waha",
                profile_name=conversation_profile_name,
            )
            media = None
            if msg.media_url and msg.msg_type in ["image", "audio"]:
                # Convert media to LangChain format
                media_item = convert_media_to_langchain_format(url=msg.media_url, media_type=msg.msg_type)

                if media_item:
                    media = [media_item]
            add_message_to_history(tenant_name=tenant_name, session_id=str(conv.id), text=body_message, media=media)
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
    """Send an email notification to all users with access to the tenant when a session status changes."""
    try:
        from maia.services.support_email import send_support_email
        from maia.models.users import UserDocument

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
                send_support_email(html_message_body=html_body, subject=subject, to=user.email)
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

    The NOWEB engine delivers the PN alongside the LID inside ``_data.key`` as
    "Alt" fields, so the mapping is available without any extra API call:

        _data.key.remoteJid    / remoteJidAlt    -> the chat (individual or group)
        _data.key.participant  / participantAlt  -> the actual sender in a group

    WEBJS chats arrive as ``@c.us`` and never reach this path. Returns the cleaned
    phone number when the given LID is found in the payload, otherwise "".
    """
    payload = data.get("payload", {}) or {}
    key = payload.get("_data", {}).get("key", {}) or {}
    lid_id = lid_string.split("@")[0]
    for lid_field, pn_field in (("remoteJid", "remoteJidAlt"), ("participant", "participantAlt")):
        lid_val = key.get(lid_field, "") or ""
        pn_val = key.get(pn_field, "") or ""
        if pn_val and "@lid" in lid_val and lid_val.split("@")[0] == lid_id:
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
    *sender's* push name (our own account, e.g. "Yayo"), NOT the person
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


def _get_or_create_group_sender_customer(
    data: dict, tenant_name: str, sender_phone: str, sender_lid: str, sender_name: str
) -> CustomerDocument:
    """Create or get an individual customer record for a group message sender.

    Stores the sender's real phone number and LID, enabling mention resolution
    and participant tracking across groups.
    """
    customer = CustomerDocument.objects(phone_number=sender_phone, tenant_name=tenant_name).first()

    if not customer:
        customer = CustomerDocument(
            tenant_name=tenant_name,
            phone_number=sender_phone,
            name=sender_name,
            platform="waha",
            created_date=datetime.now(timezone.utc).replace(microsecond=0),
            reply_mode="AI",
        )
        customer.meta_data = customer.meta_data or {}
        customer.meta_data["is_group_participant"] = True
        if sender_lid:
            customer.meta_data["lid"] = sender_lid
        try:
            customer.save()
            customer.reload()
        except NotUniqueError:
            customer = CustomerDocument.objects(phone_number=sender_phone, tenant_name=tenant_name).first()
    else:
        changed = False
        if not customer.meta_data:
            customer.meta_data = {}
            changed = True
        if sender_lid and customer.meta_data.get("lid") != sender_lid:
            customer.meta_data["lid"] = sender_lid
            changed = True
        if sender_name and sender_name != sender_phone and customer.name in [customer.phone_number, None, ""]:
            customer.name = sender_name
            changed = True
        if changed:
            customer.save()

    return customer


def _extract_mentioned_ids(payload: dict) -> list:
    """Extract the list of mentioned JIDs from a webhook payload, whatever the WAHA engine.

    WEBJS exposes them as `_data.mentionedJidList`. NOWEB (Baileys) puts the raw protocol
    message in `_data.message`, where mentions live in `<msgType>.contextInfo.mentionedJid`.
    """
    data = payload.get("_data", {}) or {}

    # WEBJS engine
    mentioned = data.get("mentionedJidList") or []
    if mentioned:
        return list(mentioned)

    # NOWEB engine — mentions can hang off any message type (text, image, video, ...)
    message = data.get("message") or {}
    if isinstance(message, dict):
        for content in message.values():
            if not isinstance(content, dict):
                continue
            mentioned = (content.get("contextInfo") or {}).get("mentionedJid") or []
            if mentioned:
                return list(mentioned)

    return []


def _resolve_mentions_in_body(data: dict, tenant_name: str, body_message: str, mentioned_ids: list) -> str:
    """Resolve mentions in message body, replacing LIDs with real phone numbers and adding names."""
    for mid in mentioned_ids:
        # Handle both string and dict JID formats
        if isinstance(mid, dict):
            mid_str = mid.get("_serialized", "")
        else:
            mid_str = str(mid)

        if not mid_str:
            continue

        mid_id = mid_str.split("@")[0]

        if "@lid" in mid_str:
            # LID mention — resolve to real phone number
            existing = CustomerDocument.objects(tenant_name=tenant_name, meta_data__lid=mid_str).first()

            if existing:
                resolved_phone = existing.phone_number
                resolved_name = existing.name
            else:
                resolved_phone = _resolve_lid_to_phone_number(data, tenant_name, mid_str)
                resolved_name = None
                if resolved_phone and resolved_phone != mid_id:
                    mentioned_customer = _get_or_create_group_sender_customer(
                        data=data,
                        tenant_name=tenant_name,
                        sender_phone=resolved_phone,
                        sender_lid=mid_str,
                        sender_name=resolved_phone,
                    )
                    resolved_name = mentioned_customer.name

            if resolved_phone and resolved_phone != mid_id:
                display = f"@{resolved_phone}"
                if resolved_name and resolved_name != resolved_phone:
                    display = f"@{resolved_phone} ({resolved_name})"
                body_message = body_message.replace(f"@{mid_id}", display)
        else:
            # Regular phone mention (e.g. @972545564449@c.us) — just add name
            phone = mid_id.replace("+", "")
            customer = CustomerDocument.objects(phone_number=phone, tenant_name=tenant_name).first()
            if customer and customer.name and customer.name != phone:
                # Replace @phone with @phone (name) if not already present
                mention_text = f"@{phone}"
                display = f"@{phone} ({customer.name})"
                if mention_text in body_message and display not in body_message:
                    body_message = body_message.replace(mention_text, display)

    return body_message


def create_or_get_customer(  # NOSONAR
    data: dict,
    tenant_name: str,
    chat_settings: ChatSettingsDocument,
    from_me: bool = False,
) -> tuple[CustomerDocument, bool]:
    """Create or retrieve customer based on WAHA Pro data structure.

    Returns:
        A tuple of (customer, is_new_customer).
    """
    payload = data.get("payload", {})

    # Check for poll vote
    is_poll_vote = False
    if data.get("event") == "poll.vote":
        is_poll_vote = True
    elif data.get("event") == "engine.event" and payload.get("event") == "vote_update":
        is_poll_vote = True

    if is_poll_vote:
        vote_data = payload.get("_data", {}) or payload.get("data", {})
        voter_field = vote_data.get("voter", "")

        # Check if parent message was in a group - try multiple paths
        parent_remote = vote_data.get("parentMessage", {}).get("_data", {}).get("id", {}).get("remote", "")
        # Also check vote.to and poll.to
        vote_to = payload.get("vote", {}).get("to", "") or payload.get("poll", {}).get("to", "")
        is_group_message = GROUP_JID_SUFFIX in (parent_remote or vote_to or "")

        # For poll votes in groups, use the GROUP as the customer (phone_number)
        # The actual voter will be stored in metadata
        if is_group_message:
            # Use the group ID as phone_number and from_field
            from_field = parent_remote or vote_to
            phone_number = from_field.split("@")[0]
            sender_name = phone_number  # Will be updated to group name below
        else:
            # For private poll votes, use the voter's number
            from_field = voter_field
            phone_number = from_field.split("@")[0]
            if "@lid" in from_field:
                phone_number = _resolve_lid_to_phone_number(data, tenant_name, from_field)
            sender_name = phone_number
    else:
        # Check if it's a group message
        from_field = payload.get("from", "")
        is_group_message = GROUP_JID_SUFFIX in from_field

        # When fromMe=True, the 'from' is our own number, so we need to use 'to' instead
        if from_me:
            # Message sent BY us, so customer is the recipient. On the WEBJS engine
            # that's payload.to, but the NOWEB engine omits `to` entirely and carries
            # the chat partner in payload.from (as @c.us or @lid). Fall back to `from`
            # so the customer keeps its real phone_number; otherwise it resolves to an
            # empty phone_number, every manual reply collapses into one shared
            # conversation, and the block_ai_response guard (which correlates manager
            # messages by phone_number + conversation_id) can never fire → the AI keeps
            # replying after a human took over.
            to_field = payload.get("to", "") or payload.get("from", "")
            phone_number = to_field.split("@")[0]
            # Check if it's a LID - try to get actual phone number
            if "@lid" in to_field:
                phone_number = _resolve_lid_to_phone_number(data, tenant_name, to_field)
        else:
            # Message received FROM someone, customer is the sender (in 'from' field)
            phone_number = from_field.split("@")[0]
            # Check if it's a LID - try to get actual phone number
            if "@lid" in from_field:
                phone_number = _resolve_lid_to_phone_number(data, tenant_name, from_field)

        # Get sender name from _data.
        # For fromMe messages, notifyName is OUR push name (the sender),
        # not the recipient — so never use it as the customer name here.
        if from_me and not is_group_message:
            sender_name = phone_number
        else:
            sender_name = _extract_push_name(payload, phone_number)
    # Update sender_name for groups
    if is_group_message:
        group_name = get_group_name(data, tenant_name)
        if group_name and group_name != "Group":
            sender_name = group_name

    customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()

    if not customer:
        # For new customers reached via an outgoing (fromMe) private chat,
        # resolve the recipient's real name (notifyName is our own push name).
        if from_me and not is_group_message:
            recipient_name = _resolve_recipient_name(data, tenant_name, payload.get("to", "") or phone_number)
            if recipient_name:
                sender_name = recipient_name
        # Determine reply mode: group chats are always blocked by default
        reply_mode = "AI"
        if is_group_message:
            reply_mode = "blocked"
        customer = CustomerDocument(
            tenant_name=tenant_name,
            phone_number=phone_number,
            name=sender_name,
            platform="waha",
            created_date=datetime.now(timezone.utc).replace(microsecond=0),
            reply_mode=reply_mode,
        )
        customer.meta_data = customer.meta_data or {}
        customer.meta_data["is_group"] = is_group_message
        if is_group_message:
            customer.meta_data["group_name"] = sender_name
            customer.meta_data["group_id"] = from_field
        is_new_customer = True
        try:
            customer.save()
            customer.reload()
        except NotUniqueError:
            is_new_customer = False
            customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    else:
        # Update name if it's empty or different
        if not customer.name and sender_name:
            customer.name = sender_name
            customer.save()
        elif customer.name == customer.phone_number and sender_name:
            customer.name = sender_name
            customer.save()
        # Also update if customer name is "Group" but we have a real name now
        elif customer.name == "Group" and sender_name and sender_name != "Group":
            customer.name = sender_name
            customer.save()
        return customer, False  # existing customer
    return customer, is_new_customer


def prepare_body_message(data, tenant_name):
    """Extract message body from WAHA Pro data structure"""
    payload = data.get("payload", {})

    # Check for poll vote
    is_poll_vote = False
    if data.get("event") == "poll.vote":
        is_poll_vote = True
    elif data.get("event") == "engine.event" and payload.get("event") == "vote_update":
        is_poll_vote = True

    if is_poll_vote:
        vote_data = payload.get("data", {})
        selected_options = vote_data.get("selectedOptions", [])
        if selected_options:
            options = [opt.get("name") for opt in selected_options if opt.get("name")]
            return ", ".join(options)
        return ""

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


def get_group_name(data, tenant_name):  # NOSONAR
    """Get group name from WAHA Pro"""
    payload = data.get("payload", {})

    # Try to get from _data.chat.name first (if available)
    group_name = payload.get("_data", {}).get("chat", {}).get("name")
    if group_name:
        return group_name

    # Determine group_id
    # For poll votes, try parent message
    vote_data = payload.get("_data", {}) or payload.get("data", {})
    if vote_data.get("parentMessage"):
        parent_group_name = vote_data.get("parentMessage", {}).get("_data", {}).get("from")
        if parent_group_name and GROUP_JID_SUFFIX in parent_group_name:
            group_id = parent_group_name
        else:
            parent_remote = vote_data.get("parentMessage", {}).get("_data", {}).get("id", {}).get("remote", "")
            if parent_remote and GROUP_JID_SUFFIX in parent_remote:
                group_id = parent_remote
            else:
                group_id = None
    else:
        group_id = payload.get("from")
        if not group_id or GROUP_JID_SUFFIX not in group_id:
            vote_to = payload.get("vote", {}).get("to", "") or payload.get("poll", {}).get("to", "")
            if vote_to and GROUP_JID_SUFFIX in vote_to:
                group_id = vote_to

    if not group_id or GROUP_JID_SUFFIX not in group_id:
        return "Group"

    session_name = data.get("session", "")
    if not session_name:
        return "Group"

    try:
        service = WahaService(tenant_name, session_name)

        # Try chats/overview first (returns "name" directly)
        chat_info = service.get_chat_info(group_id)
        chat_name = chat_info.get("name")
        if chat_name:
            return chat_name

        # Fallback to groups endpoint
        group_info = service.get_group_info(group_id)
        subject = group_info.get("groupMetadata", {}).get("subject")
        if subject:
            return subject

        return "Group"
    except Exception as e:
        logger.error(f"Error fetching group name: {e}")
        return "Group"


def handle_reaction_message(data, tenant_name):
    """Handle reaction messages by updating the original message's metadata

    WAHA reaction format:
    {
        "event": "engine.event",
        "payload": {
            "event": "message_reaction",
            "data": {
                "reaction": "👍",
                "msgId": {
                    "_serialized": "true_972529947856@c.us_3EB048640C1BB528B31DD6"
                },
                "senderId": "49006180831324@lid",
                "timestamp": 1767215356.608
            }
        }
    }
    """
    event = data.get("event")
    payload = data.get("payload", {})

    # Check if this is a reaction message
    if event == "engine.event" and payload.get("event") == "message_reaction":
        reaction_data = payload.get("data", {})

        # Get the message ID being reacted to
        msg_id_data = reaction_data.get("msgId", {})
        # Extract the actual message ID from serialized format
        serialized_id = msg_id_data.get("_serialized", "")
        msg_id = msg_id_data.get("id", "")

        # The reaction emoji
        reaction_text = reaction_data.get("reaction", "")

        if not (serialized_id or msg_id):
            logger.info("No message ID found in reaction message")
            return True

        # Try to find the original message by either serialized ID or msg_id
        search_ids = [sid for sid in [serialized_id, msg_id] if sid]
        original_message = MessageDocument.objects(tenant_name=tenant_name, sid__in=search_ids).first()

        # Also check in meta_data for fake_sid
        if not original_message:
            original_message = MessageDocument.objects(
                tenant_name=tenant_name, meta_data__fake_sid__in=search_ids
            ).first()

        if original_message:
            # Initialize reactions list if it doesn't exist
            if "reactions" not in original_message.meta_data:
                original_message.meta_data["reactions"] = []

            # Get sender info
            sender_id = reaction_data.get("senderId", "")
            sender_phone = sender_id.split("@")[0]

            # Resolve LID if needed
            if "@lid" in sender_id:
                sender_phone = _resolve_lid_to_phone_number(data, tenant_name, sender_id)

            # Get sender name (use phone as fallback)
            sender_name = sender_phone

            # Check if this sender already reacted (update reaction)
            existing_reaction_index = None
            for idx, reaction in enumerate(original_message.meta_data["reactions"]):
                if reaction.get("sender_phone") == sender_phone:
                    existing_reaction_index = idx
                    break

            # Create reaction object
            timestamp = reaction_data.get("timestamp", datetime.now(timezone.utc).timestamp())
            reaction_obj = {
                "reaction": reaction_text,
                "sender_phone": sender_phone,
                "sender_name": sender_name,
                "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            }

            if existing_reaction_index is not None:
                # Update existing reaction
                if reaction_text:  # If there's a reaction, update it
                    original_message.meta_data["reactions"][existing_reaction_index] = reaction_obj
                else:  # If reaction is empty, remove it (user removed their reaction)
                    original_message.meta_data["reactions"].pop(existing_reaction_index)
            else:
                # Add new reaction
                if reaction_text:  # Only add if there's a reaction
                    original_message.meta_data["reactions"].append(reaction_obj)

            original_message.save()
            logger.info(f"Added reaction '{reaction_text}' to message {serialized_id or msg_id}")
        else:
            logger.warning(f"Original message with ID {serialized_id or msg_id} not found")

        return True

    return False
