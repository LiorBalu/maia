"""Gmail push handler: fetch the new messages of the watched inbox, clean the reply text, save them and trigger the reply."""

import logging
import asyncio
import re
from os import getenv
from typing import Optional

from maia.inbound.html_text import html_to_text
from maia.inbound.save.common import (
    SaveResponse,
    update_conversation_profile_name,
)
from maia.models.customers import CustomerDocument
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.google_accounts import GoogleShared
from maia.utils.decode_pubsub_message import decode_pubsub_message
from maia.api.email import fetch_new_messages, get_email_service, get_message_content

logger = logging.getLogger(__name__)


def clean_email_reply(text):
    """
    Removes quoted reply text from email body.
    Handles patterns like:
    - On [date], [name] wrote:
    - From: ...
    """
    if not text:
        return ""

    # Patterns that indicate start of a reply
    patterns = [
        r"On\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec).+?wrote:",
        r"From:\s.+?To:\s.+?Subject:",
        r"Sent from my iPhone",
        r"Sent from my Android",
        r"_{10,}",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            return text[: match.start()].strip()

    return text


async def save_email_message(data: dict, tenant_name: Optional[str] = None) -> SaveResponse:
    """Save incoming email message"""

    # Handle different data structures
    try:
        try:
            # Try to decode as pubsub message first
            if isinstance(data, dict) and "message" in data and "data" in data["message"]:
                dec = decode_pubsub_message(data)
            else:
                # If it's not a pubsub message structure, use data directly
                # This handles cases where data is form data or direct JSON
                logger.info(" ".join(str(part) for part in ("Data is not in pubsub format, using directly:", data)))
                dec = data
        except Exception as e:
            logger.error(f"Error decoding pubsub message: {e}")
            # Fallback to using data directly
            dec = data

        logger.info(" ".join(str(part) for part in ("Decoded data (dec):", dec)))

        # Check if required fields are available
        history_id = None
        email_address = None

        if isinstance(dec, dict):
            history_id = dec.get("historyId")
            email_address = dec.get("emailAddress")

        platform = GoogleShared.objects(email=email_address).first()
        if not platform:
            raise Exception("Platform not found")
        tenant_name = platform.tenant_name
        service = get_email_service(platform.access_token, platform.refresh_token)
        # If we don't have the required fields, try to extract from form data
        if not history_id or not email_address:
            logger.warning(" ".join(str(part) for part in ("Missing required fields in decoded data. Available keys:", list(dec.keys()) if isinstance(dec, dict) else "Data is not a dict")))
            # Try to get values from direct data access (for non-async access)
            if isinstance(data, dict):
                history_id = history_id or data.get("historyId") or data.get("HistoryId")
                email_address = (
                    email_address or data.get("emailAddress") or data.get("To") or data.get("from") or data.get("From")
                )

        if not history_id:
            logger.warning("No historyId found in data, cannot fetch new messages")
            return SaveResponse()

        if not email_address:
            logger.warning("No emailAddress found in data, cannot proceed")
            return SaveResponse()
        try:
            new_message_ids = fetch_new_messages(service, history_id)

            # Fallback: If no messages found via history, check the latest messages
            if not new_message_ids:
                logger.warning("No messages found via history, checking latest messages...")
                try:
                    # List the most recent messages (e.g., last 10 to be safe)
                    results = service.users().messages().list(userId="me", maxResults=10).execute()
                    messages = results.get("messages", [])
                    if messages:
                        new_message_ids = [msg["id"] for msg in messages]
                        logger.info(f"Found {len(new_message_ids)} recent messages to check")
                except Exception as e:
                    logger.error(f"Error fetching latest messages: {e}")

            processed_messages = 0

            for msg_id in new_message_ids:
                message_doc = MessageDocument.objects(sid=msg_id).first()
                if message_doc:
                    continue
                try:
                    logger.info(f"Processing message ID: {msg_id}")
                    message_details = get_message_content(service, msg_id)
                    match = re.match(r"(.+?)\s*<(.+?)>", message_details["from"])
                    profile_name = ""
                    email = message_details["from"]  # Default to full from field

                    if match:
                        profile_name, email = match.groups()
                        profile_name = profile_name.strip()
                    else:
                        # If no match, try to extract just the email part
                        # Handle cases like "user@example.com" without name
                        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", message_details["from"])
                        if email_match:
                            email = email_match.group()
                            profile_name = message_details["from"].replace(email, "").strip()

                    phone_number = email

                    customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
                    if not customer:
                        customer = CustomerDocument.objects(email=email, tenant_name=tenant_name).first()
                    if not customer:
                        customer = CustomerDocument(
                            phone_number=email, email=email, tenant_name=tenant_name, platform="email"
                        )
                        customer.save()

                    body_text = html_to_text(message_details["body"])
                    cleaned_body = clean_email_reply(body_text)
                    message = f"Subject: {message_details['subject']}\n{cleaned_body}"

                    media_url = None
                    if message_details["attachments"] and len(message_details["attachments"]) > 0:
                        media_url = message_details["attachments"][0]["data"]

                    # Extract reply info
                    in_reply_to = message_details.get("in_reply_to")
                    message_id_header = message_details.get("message_id")

                    replit_message_sid = None
                    if in_reply_to:
                        # Normalize the in_reply_to (remove brackets)
                        clean_in_reply_to = in_reply_to.strip("<>")
                        # Try exact match with brackets first
                        parent_msg = MessageDocument.objects(
                            tenant_name=tenant_name, phone_number=email, sid=in_reply_to
                        ).first()
                        if not parent_msg:
                            # Try without brackets
                            parent_msg = MessageDocument.objects(
                                tenant_name=tenant_name, phone_number=email, meta_data__email_message_id=clean_in_reply_to
                            ).first()

                        if parent_msg:
                            replit_message_sid = parent_msg.sid

                    msg = MessageDocument(
                        tenant_name=tenant_name,
                        platform="email",
                        phone_number=email,
                        sender_id=str(customer.id),
                        profile_name=profile_name,
                        message=message,
                        role="user" if customer.email != email_address else "manager",
                        to=message_details["to"],
                        msg_type="text" if not media_url else "media",
                        media_url=media_url,
                        sid=msg_id,
                        message_sids=[],
                        meta_data={
                            "subject": message_details["subject"],
                            "thread_id": message_details.get("thread_id"),
                            "attachments": message_details["attachments"],
                            "email_message_id": message_id_header,
                            "in_reply_to": in_reply_to,
                            "replit_message_sid": replit_message_sid,
                            "base_message": cleaned_body,
                        },
                    )
                    msg.save()
                    msg.reload()
                    update_conversation_profile_name(tenant_name, email, profile_name, msg.conversation_id)

                    logger.info(f"From: {message_details['from']}")
                    logger.info(f"To: {message_details['to']}")
                    logger.info(f"Subject: {message_details['subject']}")
                    logger.info(f"Body:\n{body_text[:200]}\n{'-' * 50}")
                    processed_messages += 1
                    tenant = TenantDocument.objects(name=tenant_name).only("manual_mode", "status").first()
                    if tenant and (tenant.manual_mode or tenant.status == "inactive"):
                        continue

                    if customer.reply_mode != "blocked" and customer.reply_mode != "agent" and email != email_address:
                        if getenv("USE_ARQ_WORKER", "").lower() == "true":
                            # Get ARQ pool (singleton, reused across all requests)
                            from workers.pool import arq_pool_manager

                            pool = await arq_pool_manager.get_pool()
                            await pool.enqueue_job(
                                "reply_job",
                                tenant_name,
                                str(customer.email),
                                {},
                                str(msg.id),
                                False,  # single_message_mode
                                msg.conversation_id,  # conversation_id
                                _defer_by=0,
                                _job_id=f"gen:{msg.id}",
                            )
                        else:
                            from maia.inbound.reply import reply_to_message

                            asyncio.create_task(
                                reply_to_message(
                                    tenant_name=tenant_name,
                                    user_id=email,
                                    task_message_id=str(msg.id),
                                    conversation_id=msg.conversation_id,
                                )
                            )
                except Exception as e:
                    logger.error(f"An error occurred processing message {msg_id}: {e}")

                    # Check if it's a 404 error specifically
                    if "404" in str(e) and "not found" in str(e).lower():
                        logger.error(f"Message {msg_id} not found (404 error) - possibly deleted or moved")
                    elif "403" in str(e):
                        logger.error(f"Access denied to message {msg_id} (403 error) - insufficient permissions")
                    else:
                        logger.error(f"Other error type: {type(e).__name__}")

                    # Continue to next message instead of failing completely
                    continue

            if processed_messages == 0:
                logger.error("No new messages to process or all messages failed to process")
                return SaveResponse()
            # Messages were processed and responses triggered internally
            return SaveResponse()
        except Exception as e:
            logger.error(f"An error occurred fetching messages: {e}")
            return SaveResponse()
    except Exception as e:
        logger.error(f"An error occurred in normalize_email_data: {e}")
        return SaveResponse()
