"""Admin API: Gmail integration of a tenant: watch (Pub/Sub push) registration, inbox polling helpers, attachments."""

import logging
import base64
import os
import re
from datetime import datetime
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
from maia.api.security import authorize_current_user

logger = logging.getLogger(__name__)


def _extract_text_from_html(raw_html):
    """Convert HTML content to plain text"""
    if not raw_html:
        return ""
    # Simple HTML tag removal for basic conversion
    tag_pattern = re.compile("<.*?>")
    plain_text = re.sub(tag_pattern, "", raw_html)
    # Clean up extra whitespace and line breaks
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    return plain_text


GOOGLE_OAUTH_CLIENT_ID = getenv("GOOGLE_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = getenv("GOOGLE_CLIENT_SECRET")
# GCP project that owns the Pub/Sub topic for Gmail push (the service account in serviceKey.json lives there)
PUBSUB_PROJECT_ID = getenv("GCP_PROJECT_ID", "")


def build_gmail_client(access_token: str, refresh_token: str, gmail_scopes: list = None):
    # Use provided scopes or default Gmail scopes
    """Gmail API client for the stored OAuth tokens (default scopes: read, send, modify)."""
    if gmail_scopes is None:
        gmail_scopes = [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

    oauth_credentials = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=gmail_scopes,
    )
    return build("gmail", "v1", credentials=oauth_credentials)


email_router = APIRouter(prefix="/email", tags=["Email"])


def _collect_ids_from_history_records(history_records: list[dict]) -> list[str]:
    """The message ids of the Gmail history records in encounter order: each record's messagesAdded, then its
    messages field (fallback) for the ids not collected yet from any record; logs every record and every id added.
    """
    message_ids = []
    for history_record in history_records:
        logger.info(f"Processing history record: {history_record}")

        # Check for messagesAdded specifically
        if "messagesAdded" in history_record:
            for added_entry in history_record["messagesAdded"]:
                message_id = added_entry["message"]["id"]
                message_ids.append(message_id)
                logger.info(f"Added message from messagesAdded: {message_id}")

        # Also check the general messages field (fallback)
        if "messages" in history_record:
            for message_ref in history_record["messages"]:
                message_id = message_ref["id"]
                if message_id not in message_ids:  # Avoid duplicates
                    message_ids.append(message_id)
                    logger.info(f"Added message from messages: {message_id}")
    return message_ids


def fetch_new_gmail_message_ids(gmail_client, since_history_id):
    """Fetch the IDs of the new messages since a given Gmail history ID."""
    try:
        history_response = gmail_client.users().history().list(userId="me", startHistoryId=since_history_id).execute()

        message_ids = []
        logger.info(f"History response: {history_response}")

        if "history" in history_response:
            logger.info(f"Found {len(history_response['history'])} history records")
            message_ids = _collect_ids_from_history_records(history_response["history"])

        message_ids = list(set(message_ids))  # Remove duplicates
        logger.info(f"Total unique messages found: {len(message_ids)}")
        logger.info(f"Message IDs: {message_ids}")
        return message_ids

    except Exception as error:
        logger.error(f"Error fetching history: {error}")
        return []


def _read_header(message_headers: list[dict], header_name: str, default=None):
    """The value of the first message header with this name, or the default when there is none."""
    return next(
        (header["value"] for header in message_headers if header["name"] == header_name),
        default,
    )


def _extract_text_from_mime_parts(mime_parts: list[dict], depth: int = 0) -> tuple[str, str]:
    """Walk the MIME parts recursively and return (plain_text, html); the plain part wins when both exist."""
    plain_body = ""
    html_body = ""
    log_indent = "  " * depth
    logger.info(f"{log_indent}Checking {len(mime_parts)} parts at level {depth}")

    for part_index, mime_part in enumerate(mime_parts):
        mime_type = mime_part.get("mimeType", "unknown")
        logger.info(f"{log_indent}Part {part_index}: mimeType={mime_type}")
        logger.info(f"{log_indent}Part {part_index} keys: {mime_part.keys()}")

        # Check for nested parts first
        if "parts" in mime_part:
            logger.info(f"{log_indent}Part {part_index} has nested parts")
            nested_plain_body, nested_html_body = _extract_text_from_mime_parts(mime_part["parts"], depth + 1)
            if not plain_body and nested_plain_body:
                plain_body = nested_plain_body
            if not html_body and nested_html_body:
                html_body = nested_html_body

        # Check for body content
        if "body" in mime_part and mime_part["body"].get("data"):
            try:
                decoded_part_text = base64.urlsafe_b64decode(mime_part["body"]["data"]).decode("utf-8")
                logger.info(
                    f"{log_indent}Part {part_index} body decoded successfully, length: {len(decoded_part_text)}"
                )

                if mime_type == "text/plain":
                    plain_body = decoded_part_text
                    logger.info(f"{log_indent}Found plain text content: {decoded_part_text[:200]}...")
                elif mime_type == "text/html":
                    html_body = decoded_part_text
                    logger.info(f"{log_indent}Found HTML content: {decoded_part_text[:200]}...")
                else:
                    logger.info(f"{log_indent}Part {part_index} has non-text mime type: {mime_type}")
            except Exception as decode_error:
                logger.error(f"{log_indent}Error decoding part {part_index} body: {decode_error}")
        elif "body" in mime_part:
            logger.info(f"{log_indent}Part {part_index} has body but no data: {mime_part['body'].keys()}")
        else:
            logger.info(f"{log_indent}Part {part_index} has no body")

    # Return plain text if available, otherwise HTML
    if plain_body:
        return plain_body, html_body
    else:
        return html_body, html_body


def _decode_message_body(message_payload: dict) -> str:
    """The text body of a Gmail message payload: the decoded main body when it carries data, otherwise the first text
    of the MIME parts (_extract_text_from_mime_parts: the plain part, else the HTML as it is) stripped, or the HTML
    converted to text when that text is blank; empty when nothing usable is found (a main body that cannot be
    decoded is logged, not raised).
    """
    body_text = ""

    # Try to extract body content
    if "data" in message_payload.get("body", {}):
        try:
            body_text = base64.urlsafe_b64decode(message_payload["body"]["data"]).decode("utf-8")
            logger.info(f"Extracted body from main payload, length: {len(body_text)}")
        except Exception as decode_error:
            logger.error(f"Error decoding main body: {decode_error}")
    else:
        mime_parts = message_payload.get("parts", [])
        logger.info(f"Main body empty, checking {len(mime_parts)} parts")
        if mime_parts:
            plain_body, html_body = _extract_text_from_mime_parts(mime_parts)
            # Use plain text if available, otherwise convert HTML to text
            if plain_body and plain_body.strip():
                body_text = plain_body.strip()
                logger.info(f"Using extracted plain text, length: {len(body_text)}")
            elif html_body and html_body.strip():
                body_text = _extract_text_from_html(html_body).strip()
                logger.info(f"Converted HTML to text, length: {len(body_text)}")
            else:
                logger.info("No usable content found in parts")
        else:
            logger.warning("No parts found in payload")

    logger.info(f"Final body length: {len(body_text)}")
    if len(body_text) == 0:
        logger.warning("Warning: No body content extracted")
    return body_text


def fetch_gmail_message_details(gmail_client, gmail_message_id: str) -> dict:
    """Fetch and decode the content of a Gmail message."""
    try:
        # First, try to get basic message info to check if it exists
        gmail_message = gmail_client.users().messages().get(userId="me", id=gmail_message_id, format="full").execute()

        logger.info(f"Successfully fetched message {gmail_message_id}")

        # Debug: Print the entire message structure for problematic messages
        message_payload = gmail_message["payload"]
        logger.info(f"Message payload keys: {message_payload.keys()}")
        logger.info(f"Message mimeType: {message_payload.get('mimeType', 'Not specified')}")
        logger.info(f"Message body keys: {message_payload.get('body', {}).keys()}")

        message_headers = message_payload["headers"]

        attachments = _download_gmail_attachments(gmail_client, gmail_message, gmail_message_id, gmail_user_id="me")

        subject = _read_header(message_headers, "Subject", "No Subject")
        sender = _read_header(message_headers, "From", "Unknown Sender")
        recipient = _read_header(message_headers, "To", "Unknown Receiver")
        rfc_message_id = _read_header(message_headers, "Message-ID")
        in_reply_to = _read_header(message_headers, "In-Reply-To")
        references = _read_header(message_headers, "References")
        thread_id = gmail_message["threadId"]

        body_text = _decode_message_body(message_payload)

        return {
            "from": sender,
            "to": recipient,
            "subject": subject,
            "body": body_text,
            "thread_id": thread_id,
            "attachments": attachments,
            "message_id": rfc_message_id,
            "in_reply_to": in_reply_to,
            "references": references,
        }

    except Exception as error:
        logger.error(f"Error in fetch_gmail_message_details for message {gmail_message_id}: {error}")
        raise error


def _download_attachment_data(gmail_client, gmail_user_id: str, gmail_message_id: str, attachment_id: str) -> str:
    """Download one attachment of a Gmail message and return its data (base64url); decoding it once (the result is
    discarded) raises on malformed data.
    """
    attachment_response = (
        gmail_client.users()
        .messages()
        .attachments()
        .get(userId=gmail_user_id, messageId=gmail_message_id, id=attachment_id)
        .execute()
    )
    base64.urlsafe_b64decode(attachment_response["data"].encode("UTF-8"))
    return attachment_response["data"]


def _collect_attachments_from_parts(
    gmail_client, gmail_user_id: str, gmail_message_id: str, mime_parts: list[dict]
) -> list[dict]:
    """Recurse into the MIME parts and collect the ones that carry a filename. Returns {filename, mimeType, data} of
    those with an attachmentId, in walk order (a part before the parts nested in it).
    """
    collected_attachments = []
    for mime_part in mime_parts:
        if mime_part.get("filename"):
            if "attachmentId" in mime_part["body"]:
                attachment_id = mime_part["body"]["attachmentId"]
                attachment_data = _download_attachment_data(
                    gmail_client, gmail_user_id, gmail_message_id, attachment_id
                )
                collected_attachments.append(
                    {
                        "filename": mime_part["filename"],
                        "mimeType": mime_part["mimeType"],
                        "data": attachment_data,
                    }
                )
        if "parts" in mime_part:
            collected_attachments.extend(
                _collect_attachments_from_parts(gmail_client, gmail_user_id, gmail_message_id, mime_part["parts"])
            )
    return collected_attachments


def _download_gmail_attachments(gmail_client, gmail_message, gmail_message_id, gmail_user_id="me"):
    """Download every attachment of a Gmail message; returns a list of {filename, mimeType, data} or None."""
    mime_parts = gmail_message["payload"].get("parts", [])
    downloaded_attachments = _collect_attachments_from_parts(gmail_client, gmail_user_id, gmail_message_id, mime_parts)
    return downloaded_attachments if downloaded_attachments else None


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


def _is_public_host(backend_base_url: str) -> bool:
    """True when Google can reach the host of the backend base URL: neither localhost nor 127.0.0.1, and a dotted
    name.
    """
    backend_host = urlparse(backend_base_url).hostname or ""
    return not (backend_host in ("localhost", "127.0.0.1") or "." not in backend_host)


def _build_gmail_pubsub_topic_name(backend_base_url: str) -> str:
    """The id of the Pub/Sub topic Gmail pushes to: email_inbound_<slug>, the slug being the backend base URL
    without its scheme and trailing slashes, with '.', '-' and ':' replaced by '_'.
    """
    # Derive a single global topic name from the base URL domain
    # e.g. "api.example.com" -> "api_example_com"
    domain_slug = (
        backend_base_url.replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
        .replace(".", "_")
        .replace("-", "_")
        .replace(":", "_")
    )
    return f"email_inbound_{domain_slug}"


def _ensure_pubsub_topic(publisher_client, pubsub_topic_path: str) -> None:
    """Create the Pub/Sub topic when reading it fails."""
    try:
        publisher_client.get_topic(request={"topic": pubsub_topic_path})
        logger.info(f"The topic {pubsub_topic_path} already exists.")
    except Exception as error:
        logger.error(f"Topic not found, creating: {error}")
        publisher_client.create_topic(name=pubsub_topic_path)
        logger.error(f"Topic {pubsub_topic_path} created.")


def _grant_gmail_publisher_role(publisher_client, pubsub_topic_path: str) -> None:
    """Give Gmail's push service account the Pub/Sub publisher role on the topic unless it already has it."""
    # Ensure the Gmail push service account has publish rights
    topic_iam_policy = publisher_client.get_iam_policy(request={"resource": pubsub_topic_path})
    publisher_role = "roles/pubsub.publisher"
    gmail_push_iam_member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
    publisher_member_lists = [
        binding.members for binding in topic_iam_policy.bindings if binding.role == publisher_role
    ]
    if any(gmail_push_iam_member in binding_members for binding_members in publisher_member_lists):
        logger.info(f"The service account {gmail_push_iam_member} is already a publisher.")
    else:
        publisher_binding = policy_pb2.Binding(role=publisher_role, members=[gmail_push_iam_member])
        topic_iam_policy.bindings.append(publisher_binding)
        publisher_client.set_iam_policy(request={"resource": pubsub_topic_path, "policy": topic_iam_policy})


def _ensure_push_subscription(topic_name: str, pubsub_topic_path: str, push_endpoint_url: str) -> None:
    """Create the push subscription of the topic (named like the topic) delivering to the push endpoint; when it
    already exists, point its push config at the endpoint instead. Any other error is raised.
    """
    # Single shared subscription – reuse the same name as the topic
    subscriber_client = pubsub_v1.SubscriberClient()
    subscription_path = subscriber_client.subscription_path(PUBSUB_PROJECT_ID, topic_name)
    push_config = pubsub_v1.types.PushConfig(push_endpoint=push_endpoint_url)

    try:
        subscriber_client.create_subscription(
            name=subscription_path,
            topic=pubsub_topic_path,
            push_config=push_config,
            ack_deadline_seconds=30,
        )
        logger.info(f"Subscription {subscription_path} created.")
    except Exception as error:
        if "already exists" not in str(error):
            raise error
        logger.error(f"Subscription {subscription_path} already exists. Updating push config to {push_endpoint_url}")
        subscriber_client.modify_push_config(
            request={
                "subscription": subscription_path,
                "push_config": push_config,
            }
        )


def _ensure_gmail_push_pipeline(topic_name: str, push_endpoint_url: str) -> None:
    """Point the Google client libraries at serviceKey.json, then make sure the Pub/Sub topic exists, Gmail's push
    service account may publish to it and the push subscription delivers to the push endpoint. Errors propagate.
    """
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "serviceKey.json"

    publisher_client = pubsub_v1.PublisherClient()
    pubsub_topic_path = publisher_client.topic_path(PUBSUB_PROJECT_ID, topic_name)

    _ensure_pubsub_topic(publisher_client, pubsub_topic_path)
    _grant_gmail_publisher_role(publisher_client, pubsub_topic_path)
    _ensure_push_subscription(topic_name, pubsub_topic_path, push_endpoint_url)


def _start_gmail_watch(account_record: GoogleShared, watch_topic_name: str) -> dict:
    """Start the Gmail watch of the account on its INBOX, publishing to the topic; returns Gmail's watch response."""
    watch_body = {
        "labelIds": ["INBOX"],
        "topicName": watch_topic_name,
    }
    # Add watch request to Gmail API
    gmail_client = build_gmail_client(account_record.access_token, account_record.refresh_token)
    return gmail_client.users().watch(userId="me", body=watch_body).execute()


@email_router.post("/register/stream")
async def register_gmail_watch(
    http_request: Request,
    registration: RegisterEmail,
    tenant_name: Optional[str] = None,
    current_user: User = Depends(authorize_current_user),
):
    """Register Gmail push for a connected account: make sure the Pub/Sub topic and push subscription exist (pointing
    at /api/async/email), give Gmail's service account publish rights, start the Gmail watch and store the
    integration. Needs a public HTTPS BACKEND_URL.
    """
    tenant_name = tenant_name or current_user.tenants_name[0]
    # The record of the account being registered (its own tokens are needed for the watch); fall
    # back to any record of the tenant for callers that only know the tenant.
    account_record = (
        GoogleShared.objects(tenant_name=tenant_name, email=registration.email).first()
        or GoogleShared.objects(tenant_name=tenant_name).first()
    )
    if not account_record:
        return JSONResponse(
            content={"message": "No data for this email shared with our account please share calendar and try again"},
            status_code=404,
        )

    backend_base_url = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    # Pub/Sub pushes only to a public HTTPS endpoint. On a local address nothing would arrive and the
    # topic/subscription created in GCP would point at localhost, so refuse early with a clear message.
    if not _is_public_host(backend_base_url):
        return JSONResponse(
            content={
                "message": "Gmail push needs a public HTTPS BACKEND_URL (for example an ngrok domain); "
                f"'{backend_base_url}' is not reachable from Google. The account stays connected."
            },
            status_code=400,
        )
    topic_name = _build_gmail_pubsub_topic_name(backend_base_url)
    watch_topic_name = f"projects/{PUBSUB_PROJECT_ID}/topics/{topic_name}"

    # Single shared webhook endpoint – no tenant_name, no v1
    push_endpoint_url = backend_base_url.replace("http://", "https://").rstrip("/") + "/api/async/email"
    logger.info(" ".join(str(fragment) for fragment in ("webhook_url calculated:", push_endpoint_url)))
    logger.info(" ".join(str(fragment) for fragment in ("topic_name:", topic_name)))

    try:
        _ensure_gmail_push_pipeline(topic_name, push_endpoint_url)
    except Exception as error:
        logger.error(f"An error occurred setting up PubSub: {error}")
        return JSONResponse(
            content={"message": "Failed to register email", "error": str(error)},
            status_code=500,
        )

    watch_response = _start_gmail_watch(account_record, watch_topic_name)

    # Create email platform in database
    return _persist_gmail_integration(
        tenant_name=tenant_name,
        account_email=registration.email,
        watch_topic_name=watch_topic_name,
        access_token=account_record.access_token,
        refresh_token=account_record.refresh_token,
        watch_history_id=watch_response["historyId"],
    )


def _verify_email_not_owned_by_other_tenant(tenant_name: str, account_email: str) -> None:
    """Raise HTTPException 400 when the email is already linked to a different tenant."""
    # Check if this email is already linked to a DIFFERENT tenant
    record_with_same_email = GoogleShared.objects(email=account_email).first()
    if record_with_same_email and record_with_same_email.tenant_name != tenant_name:
        raise HTTPException(
            status_code=400,
            detail="Email integration already exists for another tenant",
        )


def _upsert_integration_record(tenant_name: str, account_email: str, access_token: str, refresh_token: str):
    """Store the email, the tokens and gmail_watch_enabled=True on the tenant's record of this email (else on the
    tenant's first record), or create the record when the tenant has none; returns the saved record.
    """
    # Upsert: update if an entry already exists for this tenant+email, else create
    integration_record = GoogleShared.objects(tenant_name=tenant_name, email=account_email).first()
    if not integration_record:
        # Also check by tenant alone (in case email changed)
        integration_record = GoogleShared.objects(tenant_name=tenant_name).first()

    if integration_record:
        integration_record.email = account_email
        integration_record.access_token = access_token
        integration_record.refresh_token = refresh_token
        integration_record.gmail_watch_enabled = True
        integration_record.save()
    else:
        integration_record = GoogleShared(
            tenant_name=tenant_name,
            email=account_email,
            access_token=access_token,
            refresh_token=refresh_token,
            gmail_watch_enabled=True,
        )
        integration_record.save()
    return integration_record


def _persist_gmail_integration(
    tenant_name: str,
    account_email: str,
    access_token: str,
    refresh_token: str,
    watch_topic_name: str,
    watch_history_id: str,
):
    """Save or update the GoogleShared record of the Gmail integration (tokens, gmail_watch_enabled); an email already
    linked to another tenant is refused.
    """
    _verify_email_not_owned_by_other_tenant(tenant_name, account_email)
    _upsert_integration_record(tenant_name, account_email, access_token, refresh_token)

    return JSONResponse(
        content={
            "message": "Email integration saved successfully",
            "tenant_name": tenant_name,
            "email": account_email,
            "history_id": watch_history_id,
        },
        status_code=200,
    )


def _summarize_watch_response(watch_response: dict, watch_topic_name: str) -> dict:
    """The 'watch' part of an account's status entry: historyId and expiration as Gmail returned them, the expiration
    as local ISO time, whether it has passed (True when Gmail gave none) and the topic.
    """
    expiration_ms = int(watch_response.get("expiration", 0))
    expires_at = datetime.fromtimestamp(expiration_ms / 1000) if expiration_ms else None
    checked_at = datetime.now()
    return {
        "historyId": watch_response.get("historyId"),
        "expiration": watch_response.get("expiration"),
        "expiration_human": expires_at.isoformat() if expires_at else None,
        "is_expired": expires_at < checked_at if expires_at else True,
        "topic": watch_topic_name,
    }


def _check_integration_watch(integration_record: GoogleShared, watch_topic_name: str) -> dict:
    """The status entry of one GoogleShared record: its tokens, its Gmail profile and its watch (the watch() call
    renews it), with an error text for each step that failed; missing tokens, a Gmail client that cannot be built or
    a failed getProfile end the checks.
    """
    status_entry = {
        "email": integration_record.email,
        "tenant_name": integration_record.tenant_name,
        "has_access_token": bool(integration_record.access_token),
        "has_refresh_token": bool(integration_record.refresh_token),
        "profile": None,
        "watch": None,
        "errors": [],
    }

    if not integration_record.access_token or not integration_record.refresh_token:
        status_entry["errors"].append("Missing access_token or refresh_token — cannot check watch")
        return status_entry

    # Build Gmail service for this user
    try:
        gmail_client = build_gmail_client(integration_record.access_token, integration_record.refresh_token)
    except Exception as error:
        status_entry["errors"].append(f"Failed to build Gmail service: {str(error)}")
        return status_entry

    # 1. getProfile — verifies the OAuth token still works
    try:
        gmail_profile = gmail_client.users().getProfile(userId="me").execute()
        status_entry["profile"] = {
            "emailAddress": gmail_profile.get("emailAddress"),
            "historyId": gmail_profile.get("historyId"),
        }
    except Exception as error:
        status_entry["errors"].append(f"getProfile failed: {str(error)}")
        return status_entry  # if profile fails, watch will also fail

    # 2. watch() — idempotent; returns current expiration if already watching
    try:
        watch_response = (
            gmail_client.users()
            .watch(userId="me", body={"labelIds": ["INBOX"], "topicName": watch_topic_name})
            .execute()
        )
        status_entry["watch"] = _summarize_watch_response(watch_response, watch_topic_name)
    except Exception as error:
        status_entry["errors"].append(f"watch() failed: {str(error)}")

    return status_entry


@email_router.get("/watch/all")
async def renew_and_report_gmail_watches(
    base_url_str: Optional[str] = None,
    current_user: User = Security(authorize_current_user, scopes=["staff"]),
):
    """
    Go through ALL GoogleShared entries in the DB and check each one's
    Gmail watch status via the Gmail API.

    Google has no API to list 'which emails watch a topic' — the only way
    is to query each email individually.  This endpoint does that for you.
    """
    if not base_url_str:
        base_url_str = str(getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    topic_name = _build_gmail_pubsub_topic_name(base_url_str)
    watch_topic_name = f"projects/{PUBSUB_PROJECT_ID}/topics/{topic_name}"

    # Get ALL GoogleShared entries — no filter
    integration_records = GoogleShared.objects()

    status_entries = []
    for integration_record in integration_records:
        status_entries.append(_check_integration_watch(integration_record, watch_topic_name))

    # Summary counts
    active_count = sum(
        1
        for status_entry in status_entries
        if status_entry.get("watch") and not status_entry["watch"].get("is_expired")
    )
    expired_count = sum(
        1
        for status_entry in status_entries
        if status_entry.get("watch") and status_entry["watch"].get("is_expired")
    )
    errored_count = sum(1 for status_entry in status_entries if status_entry.get("errors"))

    return JSONResponse(
        content={
            "total_emails": len(status_entries),
            "active_watches": active_count,
            "expired_watches": expired_count,
            "errored": errored_count,
            "topic_used": watch_topic_name,
            "emails": status_entries,
        },
        status_code=200,
    )


@email_router.post("/unregister/stream")
async def unregister_gmail_watch(
    registration: RegisterEmail,
    tenant_name: Optional[str] = None,
    current_user: User = Depends(authorize_current_user),
):
    """Stop the Gmail watch of an account and mark its integration as disabled."""
    tenant_name = tenant_name or current_user.tenants_name[0]
    account_record = GoogleShared.objects(tenant_name=tenant_name, email=registration.email).first()
    if not account_record:
        return JSONResponse(
            content={"message": "No data for this email shared with our account please share calendar and try again"},
            status_code=404,
        )
    gmail_client = build_gmail_client(account_record.access_token, account_record.refresh_token)
    stop_response = gmail_client.users().stop(userId="me").execute()
    logger.info(stop_response)
    account_record.update(gmail_watch_enabled=False)

    return JSONResponse(content={"message": "Email stream stopped successfully"}, status_code=200)
