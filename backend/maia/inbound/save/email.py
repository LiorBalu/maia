"""Gmail push handler: fetch the new messages of the watched inbox, clean the reply text, save them and trigger the reply."""

import logging
import asyncio
import re
from os import getenv
from typing import NamedTuple, Optional

from maia.inbound.html_text import extract_text_from_html
from maia.inbound.save.common import (
    SaveResponse,
    fill_missing_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.google_accounts import GoogleShared
from maia.utils.decode_pubsub_message import extract_pubsub_push_payload
from maia.api.email import fetch_new_gmail_message_ids, build_gmail_client, fetch_gmail_message_details

logger = logging.getLogger(__name__)


# Patterns that indicate start of a reply
QUOTED_REPLY_MARKER_PATTERNS = (
    r"On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec).+?wrote:",
    r"From:\s.+?To:\s.+?Subject:",
    r"Sent from my iPhone",
    r"Sent from my Android",
    r"_{10,}",
)


def _strip_quoted_reply(email_body):
    """
    Removes quoted reply text from email body.
    Handles patterns like:
    - On [date], [name] wrote:
    - From: ...
    """
    if not email_body:
        return ""

    for quote_marker_pattern in QUOTED_REPLY_MARKER_PATTERNS:
        quote_marker_match = re.search(quote_marker_pattern, email_body, re.IGNORECASE | re.DOTALL)
        if quote_marker_match:
            return email_body[: quote_marker_match.start()].strip()

    return email_body


def _decode_push_payload(webhook_payload: dict):
    """The payload of a Gmail push: the decoded Pub/Sub envelope when the body carries message.data, otherwise the
    body itself (also when decoding fails, which is logged). Logs the payload it returns.
    """
    try:
        # Try to decode as pubsub message first
        if isinstance(webhook_payload, dict) and "message" in webhook_payload and "data" in webhook_payload["message"]:
            decoded_payload = extract_pubsub_push_payload(webhook_payload)
        else:
            # If it's not a pubsub message structure, use data directly
            # This handles cases where data is form data or direct JSON
            logger.info(" ".join(str(fragment) for fragment in ("Data is not in pubsub format, using directly:", webhook_payload)))
            decoded_payload = webhook_payload
    except Exception as error:
        logger.error(f"Error decoding pubsub message: {error}")
        # Fallback to using data directly
        decoded_payload = webhook_payload

    logger.info(" ".join(str(fragment) for fragment in ("Decoded data (dec):", decoded_payload)))
    return decoded_payload


def _connect_gmail_account(watched_inbox_address: Optional[str]):
    """The GoogleShared account of the watched inbox and a Gmail API client built from its stored tokens, as a tuple
    (google_account, gmail_service). Raises ``Exception("Platform not found")`` when no account has that address.
    """
    google_account = GoogleShared.objects(email=watched_inbox_address).first()
    if not google_account:
        raise Exception("Platform not found")
    gmail_service = build_gmail_client(google_account.access_token, google_account.refresh_token)
    return google_account, gmail_service


def _fill_missing_push_fields(
    gmail_history_id: Optional[str], watched_inbox_address: Optional[str], decoded_payload, webhook_payload
):
    """(gmail_history_id, watched_inbox_address), completed from the raw webhook body when either is missing (with a
    warning listing the decoded keys); unchanged when both are present. It runs after the account lookup, so in
    practice only the history id can be recovered: without an emailAddress that lookup has already raised.
    """
    # If we don't have the required fields, try to extract from form data
    if not gmail_history_id or not watched_inbox_address:
        logger.warning(" ".join(str(fragment) for fragment in ("Missing required fields in decoded data. Available keys:", list(decoded_payload.keys()) if isinstance(decoded_payload, dict) else "Data is not a dict")))
        # Try to get values from direct data access (for non-async access)
        if isinstance(webhook_payload, dict):
            gmail_history_id = gmail_history_id or webhook_payload.get("historyId") or webhook_payload.get("HistoryId")
            watched_inbox_address = (
                watched_inbox_address or webhook_payload.get("emailAddress") or webhook_payload.get("To") or webhook_payload.get("from") or webhook_payload.get("From")
            )
    return gmail_history_id, watched_inbox_address


def _collect_new_gmail_message_ids(gmail_service, gmail_history_id: str) -> list[str]:
    """The ids of the Gmail messages added since the history id. When the history returns none, the ids of the 10
    latest messages of the inbox (still empty when that listing fails, which is logged).
    """
    new_gmail_message_ids = fetch_new_gmail_message_ids(gmail_service, gmail_history_id)

    # Fallback: If no messages found via history, check the latest messages
    if not new_gmail_message_ids:
        logger.warning("No messages found via history, checking latest messages...")
        try:
            # List the most recent messages (e.g., last 10 to be safe)
            recent_messages_response = gmail_service.users().messages().list(userId="me", maxResults=10).execute()
            recent_messages = recent_messages_response.get("messages", [])
            if recent_messages:
                new_gmail_message_ids = [listed_message["id"] for listed_message in recent_messages]
                logger.info(f"Found {len(new_gmail_message_ids)} recent messages to check")
        except Exception as error:
            logger.error(f"Error fetching latest messages: {error}")
    return new_gmail_message_ids


class _SenderIdentity(NamedTuple):
    """The sender of an email as read from its From header."""

    display_name: str
    email: str


def _parse_sender_header(from_header: str) -> _SenderIdentity:
    """Split a From header into the display name and the address: ``Name <address>``; else the first bare address
    found, the rest of the header being the display name; else the whole header as the address and no name.
    """
    sender_header_match = re.match(r"(.+?)\s*<(.+?)>", from_header)
    sender_display_name = ""
    sender_email = from_header  # Default to full from field

    if sender_header_match:
        sender_display_name, sender_email = sender_header_match.groups()
        sender_display_name = sender_display_name.strip()
    else:
        # If no match, try to extract just the email part
        # Handle cases like "user@example.com" without name
        bare_address_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", from_header)
        if bare_address_match:
            sender_email = bare_address_match.group()
            sender_display_name = from_header.replace(sender_email, "").strip()
    return _SenderIdentity(display_name=sender_display_name, email=sender_email)


def _ensure_email_customer(sender_email: str, tenant_name: str) -> CustomerDocument:
    """The tenant's customer for this sender: looked up by phone_number (an email customer's phone_number holds the
    sender's address), then by email; otherwise created on the email platform and saved.
    """
    customer_doc = CustomerDocument.objects(phone_number=sender_email, tenant_name=tenant_name).first()
    if not customer_doc:
        customer_doc = CustomerDocument.objects(email=sender_email, tenant_name=tenant_name).first()
    if not customer_doc:
        customer_doc = CustomerDocument(
            phone_number=sender_email, email=sender_email, tenant_name=tenant_name, platform="email"
        )
        customer_doc.save()
    return customer_doc


def _extract_first_attachment_url(attachments: Optional[list]) -> Optional[str]:
    """The ``data`` of the first attachment (the base64url content returned by the Gmail API, stored as the message's
    media_url), or None when the email has no attachment.
    """
    if attachments and len(attachments) > 0:
        return attachments[0]["data"]
    return None


def _find_parent_message_sid(tenant_name: str, sender_email: str, in_reply_to_header: Optional[str]) -> Optional[str]:
    """The sid of the saved message this email replies to: In-Reply-To matched as is against the sid, then without
    its angle brackets against the stored email_message_id. None without the header or without a match.
    """
    if not in_reply_to_header:
        return None
    # Normalize the in_reply_to (remove brackets)
    in_reply_to_bare_id = in_reply_to_header.strip("<>")
    # Try exact match with brackets first
    parent_message_doc = MessageDocument.objects(
        tenant_name=tenant_name, phone_number=sender_email, sid=in_reply_to_header
    ).first()
    if not parent_message_doc:
        # Try without brackets
        parent_message_doc = MessageDocument.objects(
            tenant_name=tenant_name, phone_number=sender_email, meta_data__email_message_id=in_reply_to_bare_id
        ).first()

    if parent_message_doc:
        return parent_message_doc.sid
    return None


def _persist_inbound_email(
    tenant_name: str,
    customer_doc: CustomerDocument,
    gmail_message: dict,
    gmail_message_id: str,
    sender_identity: _SenderIdentity,
    reply_text: str,
    message_role: str,
) -> MessageDocument:
    """Save the email as a MessageDocument of the email platform (text = ``Subject: ...`` + the reply text, the first
    attachment as media, the reply-thread meta_data), reload it for its conversation_id and fill the conversation's
    profile name when it is missing. Returns the saved document.
    """
    message_text = f"Subject: {gmail_message['subject']}\n{reply_text}"

    attachment_media_url = _extract_first_attachment_url(gmail_message["attachments"])

    # Extract reply info
    in_reply_to_header = gmail_message.get("in_reply_to")
    message_id_header = gmail_message.get("message_id")

    parent_message_sid = _find_parent_message_sid(tenant_name, sender_identity.email, in_reply_to_header)

    inbound_message_doc = MessageDocument(
        tenant_name=tenant_name,
        platform="email",
        phone_number=sender_identity.email,
        sender_id=str(customer_doc.id),
        profile_name=sender_identity.display_name,
        message=message_text,
        role=message_role,
        to=gmail_message["to"],
        msg_type="text" if not attachment_media_url else "media",
        media_url=attachment_media_url,
        sid=gmail_message_id,
        message_sids=[],
        meta_data={
            "subject": gmail_message["subject"],
            "thread_id": gmail_message.get("thread_id"),
            "attachments": gmail_message["attachments"],
            "email_message_id": message_id_header,
            "in_reply_to": in_reply_to_header,
            "replit_message_sid": parent_message_sid,
            "base_message": reply_text,
        },
    )
    inbound_message_doc.save()
    inbound_message_doc.reload()
    fill_missing_conversation_profile_name(
        tenant_name, sender_identity.email, sender_identity.display_name, inbound_message_doc.conversation_id
    )
    return inbound_message_doc


def _should_answer_email(
    tenant_name: str, customer_doc: CustomerDocument, sender_email: str, watched_inbox_address: str
) -> bool:
    """Whether the bot answers this email: the tenant (read again for every message) is not in manual mode and not
    inactive (a missing tenant document does not block), the customer's reply mode is neither blocked nor agent,
    and the sender is not the watched inbox itself.
    """
    tenant_doc = TenantDocument.objects(name=tenant_name).only("manual_mode", "status").first()
    if tenant_doc and (tenant_doc.manual_mode or tenant_doc.status == "inactive"):
        return False

    return customer_doc.reply_mode != "blocked" and customer_doc.reply_mode != "agent" and sender_email != watched_inbox_address


async def _dispatch_email_reply(
    tenant_name: str, customer_doc: CustomerDocument, saved_message_doc: MessageDocument, sender_email: str
) -> None:
    """Start the reply to a saved email: an ARQ job when USE_ARQ_WORKER is true, otherwise run_reply_pipeline as a
    background asyncio task. Both imports stay lazy, as before.
    """
    if getenv("USE_ARQ_WORKER", "").lower() == "true":
        # Get ARQ pool (singleton, reused across all requests)
        from workers.pool import arq_redis_pool_manager

        arq_pool = await arq_redis_pool_manager.ensure_pool()
        await arq_pool.enqueue_job(
            "run_reply_pipeline_job",
            tenant_name,
            str(customer_doc.email),
            None,  # agent_config_override: the worker loads the tenant's AgentDocument
            str(saved_message_doc.id),
            saved_message_doc.conversation_id,  # conversation_id
            _defer_by=0,
            _job_id=f"gen:{saved_message_doc.id}",
        )
    else:
        from maia.inbound.reply import run_reply_pipeline

        asyncio.create_task(
            run_reply_pipeline(
                tenant_name=tenant_name,
                user_id=sender_email,
                triggering_message_id=str(saved_message_doc.id),
                conversation_id=saved_message_doc.conversation_id,
            )
        )


def _log_gmail_message_error(gmail_message_id: str, error: Exception) -> None:
    """Log why one Gmail message failed, naming the likely cause (404: deleted or moved, 403: permissions)."""
    logger.error(f"An error occurred processing message {gmail_message_id}: {error}")

    # Check if it's a 404 error specifically
    if "404" in str(error) and "not found" in str(error).lower():
        logger.error(f"Message {gmail_message_id} not found (404 error) - possibly deleted or moved")
    elif "403" in str(error):
        logger.error(f"Access denied to message {gmail_message_id} (403 error) - insufficient permissions")
    else:
        logger.error(f"Other error type: {type(error).__name__}")


async def _process_gmail_message(
    gmail_service, gmail_message_id: str, tenant_name: str, watched_inbox_address: str
) -> bool:
    """Save one Gmail message, then start its reply when the tenant and the customer allow it.

    Returns True once the message is saved and its four log lines are written (also when a later step fails), False
    for a message saved before (same sid) or a failure before that point. Failures are logged, not raised; the
    duplicate check runs before the try, so its exceptions still reach the caller.
    """
    existing_message_doc = MessageDocument.objects(sid=gmail_message_id).first()
    if existing_message_doc:
        return False
    is_persisted = False
    try:
        logger.info(f"Processing message ID: {gmail_message_id}")
        gmail_message = fetch_gmail_message_details(gmail_service, gmail_message_id)
        sender_identity = _parse_sender_header(gmail_message["from"])

        customer_doc = _ensure_email_customer(sender_identity.email, tenant_name)

        plain_body_text = extract_text_from_html(gmail_message["body"])
        reply_text = _strip_quoted_reply(plain_body_text)
        message_role = "user" if customer_doc.email != watched_inbox_address else "manager"
        inbound_message_doc = _persist_inbound_email(
            tenant_name, customer_doc, gmail_message, gmail_message_id, sender_identity, reply_text, message_role
        )

        logger.info(f"From: {gmail_message['from']}")
        logger.info(f"To: {gmail_message['to']}")
        logger.info(f"Subject: {gmail_message['subject']}")
        logger.info(f"Body:\n{plain_body_text[:200]}\n{'-' * 50}")
        is_persisted = True
        if _should_answer_email(tenant_name, customer_doc, sender_identity.email, watched_inbox_address):
            await _dispatch_email_reply(tenant_name, customer_doc, inbound_message_doc, sender_identity.email)
    except Exception as error:
        # Continue to next message instead of failing completely
        _log_gmail_message_error(gmail_message_id, error)
    return is_persisted


async def save_and_answer_email_messages(webhook_payload: dict, _tenant_name: Optional[str] = None) -> SaveResponse:
    """Save incoming email message"""

    # Handle different data structures
    try:
        decoded_payload = _decode_push_payload(webhook_payload)

        # Check if required fields are available
        gmail_history_id = None
        watched_inbox_address = None

        if isinstance(decoded_payload, dict):
            gmail_history_id = decoded_payload.get("historyId")
            watched_inbox_address = decoded_payload.get("emailAddress")

        google_account, gmail_service = _connect_gmail_account(watched_inbox_address)
        tenant_name = google_account.tenant_name
        gmail_history_id, watched_inbox_address = _fill_missing_push_fields(
            gmail_history_id, watched_inbox_address, decoded_payload, webhook_payload
        )

        if not gmail_history_id:
            logger.warning("No historyId found in data, cannot fetch new messages")
            return SaveResponse()

        if not watched_inbox_address:
            logger.warning("No emailAddress found in data, cannot proceed")
            return SaveResponse()
        try:
            new_gmail_message_ids = _collect_new_gmail_message_ids(gmail_service, gmail_history_id)

            processed_count = 0

            for gmail_message_id in new_gmail_message_ids:
                if await _process_gmail_message(gmail_service, gmail_message_id, tenant_name, watched_inbox_address):
                    processed_count += 1

            if processed_count == 0:
                logger.error("No new messages to process or all messages failed to process")
                return SaveResponse()
            # Messages were processed and responses triggered internally
            return SaveResponse()
        except Exception as error:
            logger.error(f"An error occurred fetching messages: {error}")
            return SaveResponse()
    except Exception as error:
        logger.error(f"An error occurred in save_and_answer_email_messages: {error}")
        return SaveResponse()
