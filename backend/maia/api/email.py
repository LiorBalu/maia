"""Admin API: Gmail integration of a tenant: watch (Pub/Sub push) registration, inbox polling helpers, attachments."""

import logging
import base64
import os
import re
from os import getenv
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from google.cloud import pubsub_v1
from google.iam.v1 import policy_pb2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel

from maia.models.users import User
from maia.models.google_accounts import GoogleShared
from maia.api.security import verify_token_extended

logger = logging.getLogger(__name__)


def html_to_text(html_content):
    """Convert HTML content to plain text"""
    if not html_content:
        return ""
    # Simple HTML tag removal for basic conversion
    clean = re.compile("<.*?>")
    text = re.sub(clean, "", html_content)
    # Clean up extra whitespace and line breaks
    text = re.sub(r"\s+", " ", text).strip()
    return text


GOOGLE_CLIENT_ID = getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = getenv("GOOGLE_CLIENT_SECRET")
# GCP project that owns the Pub/Sub topic for Gmail push (the service account in serviceKey.json lives there)
GCP_PROJECT_ID = getenv("GCP_PROJECT_ID", "")


def get_email_service(access_token: str, refresh_token: str, scopes: list = None):
    # Use provided scopes or default Gmail scopes
    """Gmail API client for the stored OAuth tokens (default scopes: read, send, modify)."""
    if scopes is None:
        scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=scopes,
    )
    return build("gmail", "v1", credentials=creds)


email_router = APIRouter(prefix="/email", tags=["Email"])


def fetch_new_messages(service, start_history_id):
    """Fetch the IDs of the new messages since a given Gmail history ID."""
    try:
        history = service.users().history().list(userId="me", startHistoryId=start_history_id).execute()

        messages = []
        logger.info(f"History response: {history}")

        if "history" in history:
            logger.info(f"Found {len(history['history'])} history records")
            for record in history["history"]:
                logger.info(f"Processing history record: {record}")

                # Check for messagesAdded specifically
                if "messagesAdded" in record:
                    for msg_added in record["messagesAdded"]:
                        message_id = msg_added["message"]["id"]
                        messages.append(message_id)
                        logger.info(f"Added message from messagesAdded: {message_id}")

                # Also check the general messages field (fallback)
                if "messages" in record:
                    for message in record["messages"]:
                        message_id = message["id"]
                        if message_id not in messages:  # Avoid duplicates
                            messages.append(message_id)
                            logger.info(f"Added message from messages: {message_id}")

        messages = list(set(messages))  # Remove duplicates
        logger.info(f"Total unique messages found: {len(messages)}")
        logger.info(f"Message IDs: {messages}")
        return messages

    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        return []


def get_message_content(service, message_id: str) -> dict:
    """Fetch and decode the content of a Gmail message."""
    try:
        # First, try to get basic message info to check if it exists
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()

        logger.info(f"Successfully fetched message {message_id}")

        # Debug: Print the entire message structure for problematic messages
        payload = message["payload"]
        logger.info(f"Message payload keys: {payload.keys()}")
        logger.info(f"Message mimeType: {payload.get('mimeType', 'Not specified')}")
        logger.info(f"Message body keys: {payload.get('body', {}).keys()}")

        headers = payload["headers"]

        attachments = get_attachments(service, message, message_id, user_id="me")

        subject = next(
            (header["value"] for header in headers if header["name"] == "Subject"),
            "No Subject",
        )
        sender = next(
            (header["value"] for header in headers if header["name"] == "From"),
            "Unknown Sender",
        )
        receiver = next(
            (header["value"] for header in headers if header["name"] == "To"),
            "Unknown Receiver",
        )
        message_id_header = next(
            (header["value"] for header in headers if header["name"] == "Message-ID"),
            None,
        )
        in_reply_to = next(
            (header["value"] for header in headers if header["name"] == "In-Reply-To"),
            None,
        )
        references = next(
            (header["value"] for header in headers if header["name"] == "References"),
            None,
        )
        thread_id = message["threadId"]

        body = ""

        # Function to recursively extract text from parts
        def extract_text_from_parts(parts, level=0):
            """Walk the MIME parts recursively and return (plain_text, html); the plain part wins when both exist."""
            plain_text_content = ""
            html_content = ""
            indent = "  " * level
            logger.info(f"{indent}Checking {len(parts)} parts at level {level}")

            for i, part in enumerate(parts):
                mime_type = part.get("mimeType", "unknown")
                logger.info(f"{indent}Part {i}: mimeType={mime_type}")
                logger.info(f"{indent}Part {i} keys: {part.keys()}")

                # Check for nested parts first
                if "parts" in part:
                    logger.info(f"{indent}Part {i} has nested parts")
                    nested_plain, nested_html = extract_text_from_parts(part["parts"], level + 1)
                    if not plain_text_content and nested_plain:
                        plain_text_content = nested_plain
                    if not html_content and nested_html:
                        html_content = nested_html

                # Check for body content
                if "body" in part and part["body"].get("data"):
                    try:
                        part_data = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
                        logger.info(f"{indent}Part {i} body decoded successfully, length: {len(part_data)}")

                        if mime_type == "text/plain":
                            plain_text_content = part_data
                            logger.info(f"{indent}Found plain text content: {part_data[:200]}...")
                        elif mime_type == "text/html":
                            html_content = part_data
                            logger.info(f"{indent}Found HTML content: {part_data[:200]}...")
                        else:
                            logger.info(f"{indent}Part {i} has non-text mime type: {mime_type}")
                    except Exception as decode_error:
                        logger.error(f"{indent}Error decoding part {i} body: {decode_error}")
                elif "body" in part:
                    logger.info(f"{indent}Part {i} has body but no data: {part['body'].keys()}")
                else:
                    logger.info(f"{indent}Part {i} has no body")

            # Return plain text if available, otherwise HTML
            if plain_text_content:
                return plain_text_content, html_content
            else:
                return html_content, html_content

        # Try to extract body content
        if "data" in payload.get("body", {}):
            try:
                body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8")
                logger.info(f"Extracted body from main payload, length: {len(body)}")
            except Exception as decode_error:
                logger.error(f"Error decoding main body: {decode_error}")
        else:
            parts = payload.get("parts", [])
            logger.info(f"Main body empty, checking {len(parts)} parts")
            if parts:
                plain_text, html_text = extract_text_from_parts(parts)
                # Use plain text if available, otherwise convert HTML to text
                if plain_text and plain_text.strip():
                    body = plain_text.strip()
                    logger.info(f"Using extracted plain text, length: {len(body)}")
                elif html_text and html_text.strip():
                    body = html_to_text(html_text).strip()
                    logger.info(f"Converted HTML to text, length: {len(body)}")
                else:
                    logger.info("No usable content found in parts")
            else:
                logger.warning("No parts found in payload")

        logger.info(f"Final body length: {len(body)}")
        if len(body) == 0:
            logger.warning("Warning: No body content extracted")

        return {
            "from": sender,
            "to": receiver,
            "subject": subject,
            "body": body,
            "thread_id": thread_id,
            "attachments": attachments,
            "message_id": message_id_header,
            "in_reply_to": in_reply_to,
            "references": references,
        }

    except Exception as e:
        logger.error(f"Error in get_message_content for message {message_id}: {e}")
        raise e


def get_attachments(service, message, message_id, user_id="me"):
    """Download every attachment of a Gmail message; returns a list of {filename, mimeType, data} or None."""
    parts = message["payload"].get("parts", [])
    attachments = []

    def walk_parts(parts):
        """Recurse into the MIME parts and collect the ones that carry a filename."""
        for part in parts:
            if part.get("filename"):
                if "attachmentId" in part["body"]:
                    attachment_id = part["body"]["attachmentId"]
                    attachment = (
                        service.users()
                        .messages()
                        .attachments()
                        .get(userId=user_id, messageId=message_id, id=attachment_id)
                        .execute()
                    )
                    base64.urlsafe_b64decode(attachment["data"].encode("UTF-8"))
                    attachments.append(
                        {
                            "filename": part["filename"],
                            "mimeType": part["mimeType"],
                            "data": attachment["data"],
                        }
                    )
            if "parts" in part:
                walk_parts(part["parts"])

    walk_parts(parts)
    return attachments if attachments else None


class RegisterEmail(BaseModel):
    """Body of the Gmail watch registration: the account email."""
    email: str

    class Config:
        """Example payload shown in the API docs."""
        json_schema_extra = {
            "example": {
                "email": "sun.cosmetics.maia@gmail.com",
            }
        }


@email_router.post("/register/stream")
async def register_email(
    request: Request,
    register_email: RegisterEmail,
    tenant_name: Optional[str] = None,
    user: User = Depends(verify_token_extended),
):
    """Register Gmail push for a connected account: make sure the Pub/Sub topic and push subscription exist (pointing
    at /api/async/email), give Gmail's service account publish rights, start the Gmail watch and store the
    integration. Needs a public HTTPS BACKEND_URL.
    """
    tenant_name = tenant_name or user.tenants_name[0]
    # The record of the account being registered (its own tokens are needed for the watch); fall
    # back to any record of the tenant for callers that only know the tenant.
    calendar = (
        GoogleShared.objects(tenant_name=tenant_name, email=register_email.email).first()
        or GoogleShared.objects(tenant_name=tenant_name).first()
    )
    if not calendar:
        return JSONResponse(
            content={"message": "No data for this email shared with our account please share calendar and try again"},
            status_code=404,
        )

    # Derive a single global topic name from the base URL domain
    # e.g. "api.example.com" -> "api_example_com"
    base_url_str = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    # Pub/Sub pushes only to a public HTTPS endpoint. On a local address nothing would arrive and the
    # topic/subscription created in GCP would point at localhost, so refuse early with a clear message.
    public_host = urlparse(base_url_str).hostname or ""
    if public_host in ("localhost", "127.0.0.1") or "." not in public_host:
        return JSONResponse(
            content={
                "message": "Gmail push needs a public HTTPS BACKEND_URL (for example an ngrok domain); "
                f"'{base_url_str}' is not reachable from Google. The account stays connected."
            },
            status_code=400,
        )
    domain = (
        base_url_str.replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
        .replace(".", "_")
        .replace("-", "_")
        .replace(":", "_")
    )
    topic_name = f"email_inbound_{domain}"
    email_topic_path = f"projects/{GCP_PROJECT_ID}/topics/{topic_name}"

    # Single shared webhook endpoint – no tenant_name, no v1
    webhook_url = base_url_str.replace("http://", "https://").rstrip("/") + "/api/async/email"
    logger.info(" ".join(str(part) for part in ("webhook_url calculated:", webhook_url)))
    logger.info(" ".join(str(part) for part in ("topic_name:", topic_name)))

    try:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "serviceKey.json"

        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(GCP_PROJECT_ID, topic_name)

        try:
            publisher.get_topic(request={"topic": topic_path})
            logger.info(f"The topic {topic_path} already exists.")
        except Exception as e:
            logger.error(f"Topic not found, creating: {e}")
            publisher.create_topic(name=topic_path)
            logger.error(f"Topic {topic_path} created.")

        # Ensure the Gmail push service account has publish rights
        policy = publisher.get_iam_policy(request={"resource": topic_path})
        role = "roles/pubsub.publisher"
        service_account_email = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
        existing_members = [binding.members for binding in policy.bindings if binding.role == role]
        if any(service_account_email in members for members in existing_members):
            logger.info(f"The service account {service_account_email} is already a publisher.")
        else:
            new_binding = policy_pb2.Binding(role=role, members=[service_account_email])
            policy.bindings.append(new_binding)
            publisher.set_iam_policy(request={"resource": topic_path, "policy": policy})

        # Single shared subscription – reuse the same name as the topic
        subscriber = pubsub_v1.SubscriberClient()
        subscription_path = subscriber.subscription_path(GCP_PROJECT_ID, topic_name)
        push_config = pubsub_v1.types.PushConfig(push_endpoint=webhook_url)

        try:
            subscriber.create_subscription(
                name=subscription_path,
                topic=topic_path,
                push_config=push_config,
                ack_deadline_seconds=30,
            )
            logger.info(f"Subscription {subscription_path} created.")
        except Exception as e:
            if "already exists" not in str(e):
                raise e
            logger.error(f"Subscription {subscription_path} already exists. Updating push config to {webhook_url}")
            subscriber.modify_push_config(
                request={
                    "subscription": subscription_path,
                    "push_config": push_config,
                }
            )
    except Exception as e:
        logger.error(f"An error occurred setting up PubSub: {e}")
        return JSONResponse(
            content={"message": "Failed to register email", "error": str(e)},
            status_code=500,
        )

    watch_request = {
        "labelIds": ["INBOX"],
        "topicName": email_topic_path,
    }
    # Add watch request to Gmail API
    service = get_email_service(calendar.access_token, calendar.refresh_token)
    response = service.users().watch(userId="me", body=watch_request).execute()

    # Create email platform in database
    return create_email_integration(
        tenant_name=tenant_name,
        email=register_email.email,
        topic=email_topic_path,
        access_token=calendar.access_token,
        refresh_token=calendar.refresh_token,
        history_id=response["historyId"],
    )


def create_email_integration(
    tenant_name: str,
    email: str,
    access_token: str,
    refresh_token: str,
    topic: str,
    history_id: str,
):
    # Check if this email is already linked to a DIFFERENT tenant
    """Save or update the GoogleShared record of the Gmail integration (tokens, gmail_watch_enabled); an email already
    linked to another tenant is refused.
    """
    existing_by_email = GoogleShared.objects(email=email).first()
    if existing_by_email and existing_by_email.tenant_name != tenant_name:
        raise HTTPException(
            status_code=400,
            detail="Email integration already exists for another tenant",
        )

    # Upsert: update if an entry already exists for this tenant+email, else create
    email_integration = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    if not email_integration:
        # Also check by tenant alone (in case email changed)
        email_integration = GoogleShared.objects(tenant_name=tenant_name).first()

    if email_integration:
        email_integration.email = email
        email_integration.access_token = access_token
        email_integration.refresh_token = refresh_token
        email_integration.gmail_watch_enabled = True
        email_integration.save()
    else:
        email_integration = GoogleShared(
            tenant_name=tenant_name,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            gmail_watch_enabled=True,
        )
        email_integration.save()

    return JSONResponse(
        content={
            "message": "Email integration saved successfully",
            "tenant_name": tenant_name,
            "email": email,
            "history_id": history_id,
        },
        status_code=200,
    )


@email_router.get("/watch/all")
async def get_all_email_watches(
    base_url_str: Optional[str] = None,
    user: User = Security(verify_token_extended, scopes=["staff"]),
):
    """
    Go through ALL GoogleShared entries in the DB and check each one's
    Gmail watch status via the Gmail API.

    Google has no API to list 'which emails watch a topic' — the only way
    is to query each email individually.  This endpoint does that for you.
    """
    from datetime import datetime

    if not base_url_str:
        base_url_str = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    domain = (
        base_url_str.replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
        .replace(".", "_")
        .replace("-", "_")
        .replace(":", "_")
    )
    topic_name = f"email_inbound_{domain}"
    email_topic_path = f"projects/{GCP_PROJECT_ID}/topics/{topic_name}"

    # Get ALL GoogleShared entries — no filter
    all_integrations = GoogleShared.objects()

    results = []
    for integration in all_integrations:
        entry = {
            "email": integration.email,
            "tenant_name": integration.tenant_name,
            "has_access_token": bool(integration.access_token),
            "has_refresh_token": bool(integration.refresh_token),
            "profile": None,
            "watch": None,
            "errors": [],
        }

        if not integration.access_token or not integration.refresh_token:
            entry["errors"].append("Missing access_token or refresh_token — cannot check watch")
            results.append(entry)
            continue

        # Build Gmail service for this user
        try:
            service = get_email_service(integration.access_token, integration.refresh_token)
        except Exception as e:
            entry["errors"].append(f"Failed to build Gmail service: {str(e)}")
            results.append(entry)
            continue

        # 1. getProfile — verifies the OAuth token still works
        try:
            profile = service.users().getProfile(userId="me").execute()
            entry["profile"] = {
                "emailAddress": profile.get("emailAddress"),
                "historyId": profile.get("historyId"),
            }
        except Exception as e:
            entry["errors"].append(f"getProfile failed: {str(e)}")
            results.append(entry)
            continue  # if profile fails, watch will also fail

        # 2. watch() — idempotent; returns current expiration if already watching
        try:
            watch_resp = (
                service.users()
                .watch(userId="me", body={"labelIds": ["INBOX"], "topicName": email_topic_path})
                .execute()
            )
            exp_ms = int(watch_resp.get("expiration", 0))
            exp_dt = datetime.fromtimestamp(exp_ms / 1000) if exp_ms else None
            now = datetime.now()
            entry["watch"] = {
                "historyId": watch_resp.get("historyId"),
                "expiration": watch_resp.get("expiration"),
                "expiration_human": exp_dt.isoformat() if exp_dt else None,
                "is_expired": exp_dt < now if exp_dt else True,
                "topic": email_topic_path,
            }
        except Exception as e:
            entry["errors"].append(f"watch() failed: {str(e)}")

        results.append(entry)

    # Summary counts
    active = sum(1 for r in results if r.get("watch") and not r["watch"].get("is_expired"))
    expired = sum(1 for r in results if r.get("watch") and r["watch"].get("is_expired"))
    errored = sum(1 for r in results if r.get("errors"))

    return JSONResponse(
        content={
            "total_emails": len(results),
            "active_watches": active,
            "expired_watches": expired,
            "errored": errored,
            "topic_used": email_topic_path,
            "emails": results,
        },
        status_code=200,
    )


@email_router.post("/unregister/stream")
async def unregister_email(
    register_email: RegisterEmail,
    tenant_name: Optional[str] = None,
    user: User = Depends(verify_token_extended),
):
    """Stop the Gmail watch of an account and mark its integration as disabled."""
    tenant_name = tenant_name or user.tenants_name[0]
    calendar = GoogleShared.objects(tenant_name=tenant_name, email=register_email.email).first()
    if not calendar:
        return JSONResponse(
            content={"message": "No data for this email shared with our account please share calendar and try again"},
            status_code=404,
        )
    service = get_email_service(calendar.access_token, calendar.refresh_token)
    response = service.users().stop(userId="me").execute()
    logger.info(response)
    calendar.update(gmail_watch_enabled=False)

    return JSONResponse(content={"message": "Email stream stopped successfully"}, status_code=200)
