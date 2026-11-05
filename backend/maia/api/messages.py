"""Admin API: conversations and messages of a tenant: inbox lists (infinite scroll), send as manager, end a conversation."""

import logging
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from mongoengine import Q
from pydantic import BaseModel

from maia.services.registry import build_platform_sender_service
from maia.models.customers import CustomerDocument
from maia.models.conversations import Conversation
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.users import User, UserDocument
from maia.api.security import authenticate_current_user
from maia.utils.conversation.reset import end_customer_conversation
from maia.utils.conversations_filters import build_conversation_filter_query
from maia.utils.google_cloud_storage.utils.save_fastapi_file_in_bucket import upload_fastapi_file_to_bucket

logger = logging.getLogger(__name__)


class InfiniteScrollResponse(BaseModel):
    """One page of the inbox list: conversations, the cursor of the next page and counts."""
    conversations: List[Dict[str, Any]]
    next_cursor: Optional[str] = None
    has_more: bool = False
    total_count: int = 0  # Total matching the filter
    page_count: int = 0  # Count in current page


messages_router = APIRouter(prefix="/messages", tags=["Messages"])
conversations_router = APIRouter(prefix="/conversations", tags=["Conversations"])


@conversations_router.put("/{tenant_name}/{phone_number}/{conversation_id}/readed")
async def mark_conversation_read(
    tenant_name: str, conversation_id: str, phone_number: str, current_user: User = Depends(authenticate_current_user)
):
    """Mark a conversation as read by the manager (the save broadcasts the change over SSE)."""
    conversation_doc = Conversation.objects(tenant_name=tenant_name, id=conversation_id).first()
    if not conversation_doc:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation_doc.read_by_manager = True
    conversation_doc.save()
    return JSONResponse(status_code=200, content={"message": "Conversation updated"})


def normalize_phone_to_international_digits(raw_phone_number: str) -> Optional[str]:
    """Strip the whatsapp: prefix, '+', spaces and dashes; an Israeli local number (leading 0) becomes its 972 form.
    """
    if not raw_phone_number:
        return None
    if raw_phone_number.startswith("whatsapp:+"):
        raw_phone_number = raw_phone_number.replace("whatsapp:+", "")
    raw_phone_number = raw_phone_number.replace("+", "").replace(" ", "").replace("-", "")
    if raw_phone_number.startswith("0"):
        raw_phone_number = "972" + raw_phone_number[1:]
    return raw_phone_number


async def _parse_outgoing_message_body(
    http_request: Request,
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[UploadFile]]:
    """Read the dashboard's body in either shape: JSON (a bad or non-object body counts as empty) or a multipart
    form, whose "file" part is taken when it is an upload. Returns (message text, sid of the message replied to,
    conversation id, uploaded file).
    """
    request_content_type = (http_request.headers.get("content-type") or "").lower()
    message_text: Optional[str] = None
    replied_to_message_sid: Optional[str] = None
    conversation_id: Optional[str] = None
    uploaded_file: Optional[UploadFile] = None

    if request_content_type.startswith("application/json"):
        try:
            json_body = await http_request.json()
        except Exception:
            json_body = {}
        if not isinstance(json_body, dict):
            json_body = {}
        message_text = json_body.get("message")
        replied_to_message_sid = json_body.get("reply_to_message_id")
        conversation_id = json_body.get("conversation_id")
    else:
        multipart_form = await http_request.form()
        message_text = multipart_form.get("message")
        replied_to_message_sid = multipart_form.get("reply_to_message_id")
        conversation_id = multipart_form.get("conversation_id")
        form_file_field = multipart_form.get("file")
        if hasattr(form_file_field, "filename"):
            uploaded_file = form_file_field
    return message_text, replied_to_message_sid, conversation_id, uploaded_file


def _ensure_customer_record(tenant_name: str, phone_number: str, platform: str) -> CustomerDocument:
    """The tenant's CustomerDocument for the number, created (named after the number) and saved when missing."""
    customer_doc = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    if not customer_doc:
        customer_doc = CustomerDocument(
            name=phone_number, phone_number=phone_number, tenant_name=tenant_name, platform=platform
        )
        customer_doc.save()
    return customer_doc


@messages_router.post(
    "/{tenant_name}/{phone_number}/{sender_number}",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "reply_to_message_id": {"type": "string"},
                            "conversation_id": {"type": "string"},
                        },
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"},
                            "reply_to_message_id": {"type": "string"},
                            "conversation_id": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                },
            }
        }
    },
)
async def send_message_from_dashboard(
    http_request: Request,
    tenant_name: str,
    phone_number: str,
    background_tasks: BackgroundTasks,
    sender_number: Optional[str] = None,
    current_user: User = Depends(authenticate_current_user),
    platform: str = Query("waha"),
    role: str = Query("manager"),
):
    """Send a message from the dashboard to a customer (JSON, or multipart with a file);
    deliver_and_record_dashboard_message sends and saves it.
    """
    message_text, replied_to_message_sid, conversation_id, uploaded_file = await _parse_outgoing_message_body(
        http_request
    )

    if not message_text and uploaded_file is None:
        raise HTTPException(status_code=422, detail="message or file is required")
    # Upload the file to the sender service
    # Don't normalize  IDs (IGIDs) - they are not phone numbers
    if platform != "email":
        phone_number = normalize_phone_to_international_digits(phone_number)
        sender_number = normalize_phone_to_international_digits(sender_number)
    media_url = None
    if uploaded_file is not None:
        _uploaded_message_type, media_url = await upload_fastapi_file_to_bucket(
            f"{tenant_name}/customers/{phone_number}", uploaded_file
        )
    customer = _ensure_customer_record(tenant_name, phone_number, platform)
    await deliver_and_record_dashboard_message(
        tenant_name=tenant_name,
        recipient_address=phone_number,
        message_text=message_text,
        business_sender_address=sender_number,
        media_url=media_url,
        current_user=current_user,
        platform=platform,
        sender_role=role,
        customer=customer,
        replied_to_message_sid=replied_to_message_sid,
        conversation_id=conversation_id,
    )
    logger.info(f"[send_message_from_dashboard] platform={platform} conversation_id={conversation_id} reply_to={replied_to_message_sid}")

    return JSONResponse(status_code=200, content={"message": "Message is being sent", "phone_number": phone_number})


def convert_markdown_to_whatsapp_markup(markdown_text: str) -> str:
    """Turn markdown emphasis (**, __, double backticks) into WhatsApp formatting (*, _, backtick)."""
    return str(markdown_text).replace("**", "*").replace("__", "_").replace("``", "`") if markdown_text else ""


@dataclass(frozen=True)
class _OutgoingMessageTarget:
    """Who sends a dashboard message to whom, on which platform and in which conversation: the fields its
    MessageDocuments (the text and the file) share.
    """

    tenant_name: str
    current_user: UserDocument
    recipient_address: str
    business_sender_address: Optional[str]
    platform: str
    sender_role: str
    conversation_id: Optional[str]


def _build_outgoing_message_doc(
    outgoing_target: _OutgoingMessageTarget,
    message_text: str,
    sent_message_sid: str,
    media_url: Optional[str],
    meta_data: Optional[Dict[str, Any]],
) -> MessageDocument:
    """The MessageDocument (not saved yet) of one part of a dashboard message, its text or its file, dated now and
    signed with the dashboard user's name.
    """
    return MessageDocument(
        profile_name=outgoing_target.current_user.name,
        date=datetime.now(timezone.utc),
        phone_number=outgoing_target.recipient_address,
        message=message_text,
        role=outgoing_target.sender_role,
        sid=sent_message_sid,
        tenant_name=outgoing_target.tenant_name,
        to=outgoing_target.business_sender_address,
        platform=outgoing_target.platform,
        media_url=media_url,
        meta_data=meta_data,
        conversation_id=outgoing_target.conversation_id,
    )


def _resolve_thread_context(
    conversation_id: Optional[str], replied_to_message_sid: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Settle the conversation and the email thread of a dashboard message: the replied-to message gives the
    conversation (with its thread id and subject), the conversation's meta_data the thread id, and its newest message
    with a subject the subject. Returns (conversation id, thread id, subject).
    """
    email_thread_id = None
    email_subject = None

    # Resolve conversation_id and thread context from replied_to_message_sid
    if not conversation_id and replied_to_message_sid:
        replied_to_message_doc = MessageDocument.objects(sid=replied_to_message_sid).first()
        if replied_to_message_doc and replied_to_message_doc.conversation_id:
            conversation_id = replied_to_message_doc.conversation_id
            if replied_to_message_doc.meta_data:
                email_thread_id = replied_to_message_doc.meta_data.get("thread_id")
                email_subject = replied_to_message_doc.meta_data.get("subject")
    # If conversation_id is provided, try to get thread context from the conversation
    if conversation_id and not email_thread_id:
        conversation_doc = Conversation.objects(id=conversation_id).first()
        if conversation_doc and conversation_doc.meta_data:
            email_thread_id = conversation_doc.meta_data.get("thread_id")
    # Resolve subject from the last message in the conversation if still missing
    if email_thread_id and not email_subject and conversation_id:
        latest_subject_message_doc = (
            MessageDocument.objects(conversation_id=conversation_id, meta_data__subject__exists=True)
            .order_by("-date")
            .first()
        )
        if latest_subject_message_doc and latest_subject_message_doc.meta_data:
            email_subject = latest_subject_message_doc.meta_data.get("subject")
    return conversation_id, email_thread_id, email_subject


def _resolve_outbound_service(tenant_name: str, platform: str, business_sender_address: Optional[str]):
    """The tenant's sender service of the platform (EmailPlatformService, or WahaService for the business number).
    An HTTPException passes through; any other failure is logged and becomes a 424 asking to connect the channel.
    """
    try:
        return build_platform_sender_service(tenant_name, "sender_service", platform=platform, to=business_sender_address)
    except HTTPException:
        raise
    except Exception as sender_error:
        # No connected/usable sender for this tenant+platform (missing account, unknown
        # sender_number, bad credentials). Surface it as an actionable error instead of a bare 500.
        logger.error(f"Failed to resolve sender service for {tenant_name}/{platform}/{business_sender_address}: {sender_error}")
        raise HTTPException(
            status_code=424,
            detail=(
                f"No connected messaging sender for tenant '{tenant_name}' on platform '{platform}'"
                f"{f' (sender_number={business_sender_address})' if business_sender_address else ''}. "
                f"Connect the channel before sending. Underlying error: {sender_error}"
            ),
        )


def _deliver_outgoing_text(
    sender_service,
    recipient_address: str,
    message_text: str,
    replied_to_message_sid: Optional[str],
    email_thread_id: Optional[str],
    email_subject: Optional[str],
) -> tuple[str, Optional[str]]:
    """Send the text (markdown turned into WhatsApp markup) with the reply-to sid and, when known, the thread id and
    subject. Returns (sid of the sent message, thread id: the known one, else the one of the send response).
    """
    send_options = {"reply_to_message_id": replied_to_message_sid}
    if email_thread_id:
        send_options["thread_id"] = email_thread_id
    if email_subject:
        send_options["subject"] = email_subject
    text_send_result = sender_service.deliver_message_to_customer(
        recipient_address, message_text=convert_markdown_to_whatsapp_markup(message_text), **send_options
    )
    sent_message_sid = text_send_result["sid"]
    # Capture thread_id from send response
    if not email_thread_id and text_send_result.get("thread_id"):
        email_thread_id = text_send_result["thread_id"]
    return sent_message_sid, email_thread_id


def _deliver_and_record_outgoing_file(
    sender_service, outgoing_target: _OutgoingMessageTarget, media_url: str
) -> Optional[MessageDocument]:
    """Send the uploaded file and save its MessageDocument (empty text, the file's url); None when the send or the
    save failed (logged with the traceback, not raised).
    """
    try:
        # The sender service expects media_url as a parameter, not base64 data
        # The service should handle downloading and encoding the file
        file_send_result = sender_service.deliver_file_to_customer(
            outgoing_target.recipient_address, message_text="", media_url=media_url
        )
        file_message_doc = _build_outgoing_message_doc(
            outgoing_target, message_text="", sent_message_sid=file_send_result["sid"], media_url=media_url, meta_data=None
        )
        file_message_doc.save()
        file_message_doc.reload()
    except Exception as file_send_error:
        logger.error(f"Failed to send file via {outgoing_target.platform}: {str(file_send_error)}")
        traceback.print_exc()
        return None
    return file_message_doc


def _record_outgoing_text_message(
    outgoing_target: _OutgoingMessageTarget,
    message_text: str,
    sent_message_sid: str,
    replied_to_message_sid: Optional[str],
    email_thread_id: Optional[str],
    email_subject: Optional[str],
) -> MessageDocument:
    """Save the text's MessageDocument (reply-to sid, thread id and subject in meta_data, None when all are empty),
    then store the thread id on the conversation the save resolved. Returns the saved document.
    """
    text_message_meta_data = {}
    if replied_to_message_sid:
        text_message_meta_data["reply_to_message_id"] = replied_to_message_sid
    if email_thread_id:
        text_message_meta_data["thread_id"] = email_thread_id
    if email_subject:
        text_message_meta_data["subject"] = email_subject

    text_message_doc = _build_outgoing_message_doc(
        outgoing_target,
        message_text=message_text,
        sent_message_sid=sent_message_sid,
        media_url=None,
        meta_data=text_message_meta_data if text_message_meta_data else None,
    )
    text_message_doc.save()
    # Store the thread_id on the conversation for routing
    if email_thread_id and text_message_doc.conversation_id:
        from maia.inbound.save.common import persist_conversation_thread_id

        persist_conversation_thread_id(text_message_doc.conversation_id, email_thread_id)
    return text_message_doc


def _record_manager_text_in_history(
    tenant_name: str,
    sender_role: str,
    message_text: Optional[str],
    recorded_message_doc: Optional[MessageDocument],
    recipient_address: str,
) -> None:
    """Add a manager / agent text to its conversation's Redis history as an assistant turn and flag the saved message
    langchain_synced; a failure is logged and ignored (non-critical).
    """
    try:
        if message_text and sender_role in ["manager", "agent"] and recorded_message_doc:
            tenant_doc = TenantDocument.objects(name=tenant_name).first()
            if tenant_doc:
                online_conversation_doc = Conversation.objects(id=str(recorded_message_doc.conversation_id)).first()
                if online_conversation_doc:
                    from maia.knowledge.chat_history import record_assistant_message_in_history

                    record_assistant_message_in_history(
                        tenant_name=tenant_name, history_session_id=str(online_conversation_doc.id), assistant_message_text=f"[Manager]: {message_text}"
                    )
                    if recorded_message_doc:
                        recorded_message_doc.meta_data = recorded_message_doc.meta_data or {}
                        recorded_message_doc.meta_data["langchain_synced"] = True
                        recorded_message_doc._mark_as_changed("meta_data")
                        recorded_message_doc.save()
                    logger.info(f"Manager message saved to Redis history for {recipient_address}")
    except Exception as history_sync_error:
        logger.error(f"Could not save manager message to Redis (non-critical): {history_sync_error}")


async def deliver_and_record_dashboard_message(
    tenant_name: str,
    recipient_address: str,
    message_text: str,
    business_sender_address: str,
    platform: str,
    sender_role: str,
    current_user: UserDocument,
    replied_to_message_sid: Optional[str] = None,
    media_url: Optional[str] = None,
    customer: Optional[CustomerDocument] = None,
    conversation_id: Optional[str] = None,
):
    """Send a manager message on WhatsApp (WAHA) or email and save it: resolve the thread (reply-to message or
    conversation), send the text and the file, write the MessageDocument, add the text to the Redis history as a
    manager turn and broadcast the conversation over SSE. 424 when no sender is connected.
    """
    try:
        # await asyncio.sleep(20)  # Simulate some processing delay
        conversation_id, email_thread_id, email_subject = _resolve_thread_context(conversation_id, replied_to_message_sid)

        logger.info(f"[deliver_and_record_dashboard_message] platform={platform} conv={conversation_id} thread={email_thread_id} subject={email_subject}")

        outgoing_target = _OutgoingMessageTarget(
            tenant_name=tenant_name,
            current_user=current_user,
            recipient_address=recipient_address,
            business_sender_address=business_sender_address,
            platform=platform,
            sender_role=sender_role,
            conversation_id=conversation_id,
        )
        sent_message_sid = ""
        file_message_doc = None

        if platform in ["email", "waha"]:
            sender_service = _resolve_outbound_service(tenant_name, platform, business_sender_address)

            # Send text message only if there's a message to send
            if message_text:
                sent_message_sid, email_thread_id = _deliver_outgoing_text(
                    sender_service, recipient_address, message_text, replied_to_message_sid, email_thread_id, email_subject
                )
            if media_url:
                file_message_doc = _deliver_and_record_outgoing_file(sender_service, outgoing_target, media_url)
        recorded_message_doc = None
        if message_text:
            recorded_message_doc = _record_outgoing_text_message(
                outgoing_target, message_text, sent_message_sid, replied_to_message_sid, email_thread_id, email_subject
            )

        # When only a file was sent (no text message), use the file_message_doc as recorded_message_doc
        if not recorded_message_doc and media_url and file_message_doc:
            recorded_message_doc = file_message_doc

        # Save the manager message into the Redis history so the AI sees it as context in the
        # next user turn.
        _record_manager_text_in_history(tenant_name, sender_role, message_text, recorded_message_doc, recipient_address)
        # The message save updated and saved the conversation, which broadcast it over SSE.

        if recorded_message_doc:
            return JSONResponse(status_code=200, content=recorded_message_doc.to_json())
        return JSONResponse(status_code=200, content={"message": "Message sent", "phone_number": recipient_address})
    except HTTPException:
        raise
    except Exception as delivery_error:
        # Never let an unexpected failure surface as a bare 500 with no body.
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to send message on platform '{platform}': {delivery_error}")


@conversations_router.put("/{tenant_name}/{phone_number}/end-current-conversation")
async def end_online_conversation(tenant_name: str, phone_number: str, current_user: str = Depends(authenticate_current_user)):
    """Close the customer's online conversation (end_customer_conversation)."""
    online_conversation_doc = Conversation.objects(
        tenant_name=tenant_name, phone_number=phone_number, conversation_status="online"
    ).first()
    if not online_conversation_doc:
        raise HTTPException(status_code=404, detail="Online conversation not found")
    await end_customer_conversation(
        tenant_name=tenant_name,
        sender_id=phone_number,
        conversation_id=str(online_conversation_doc.id) if online_conversation_doc else None,
    )
    return {"message": "Conversation ended"}


def _resolve_sort_field(sort_by: Optional[str], sort_order: Optional[str]) -> tuple[str, str]:
    """The field the inbox is sorted by (an unknown one falls back to last_message_date) and its order_by prefix:
    '-' for descending (the default), '' for ascending.
    """
    # Define allowed sorting fields
    allowed_sort_fields = ["date", "last_message_date", "phone_number", "conversation_status"]

    # Validate sorting field
    sort_field = sort_by if sort_by in allowed_sort_fields else "last_message_date"

    # Determine sorting order
    sort_direction_prefix = "-" if (sort_order or "desc").lower() == "desc" else ""
    return sort_field, sort_direction_prefix


def _find_conversation_ids_by_text(tenant_name: str, search_text: str) -> Optional[List[ObjectId]]:
    """The ids of the tenant's conversations that have a message matching the text (MongoDB text index); None when
    no message matches.
    """
    # Use MongoDB text index for efficient full-text search instead of $regex
    matching_conversation_ids = (
        MessageDocument.objects(tenant_name=tenant_name).search_text(search_text).distinct("conversation_id")
    )
    if not matching_conversation_ids:
        return None
    return [
        ObjectId(matching_conversation_id)
        for matching_conversation_id in matching_conversation_ids
        if matching_conversation_id
    ]


def _filter_by_object_id_cursor(conversations_query, cursor_object_id: ObjectId, sort_order: Optional[str]):
    """Keep the conversations past the cursor's id: lower ids when sort_order is exactly "desc", higher otherwise."""
    if sort_order == "desc":
        return conversations_query.filter(id__lt=cursor_object_id)
    else:
        return conversations_query.filter(id__gt=cursor_object_id)


def _filter_by_date_cursor(
    conversations_query,
    sort_field: str,
    sort_order: Optional[str],
    cursor_datetime: datetime,
    cursor_object_id: ObjectId,
):
    """Keyset condition of a date sort: past the cursor's date, or on the same date past the cursor's id (older and
    lower when sort_order is exactly "desc", newer and higher otherwise).
    """
    if sort_order == "desc":
        # For desc: next page has older dates OR same date with smaller IDs
        return conversations_query.filter(
            Q(**{f"{sort_field}__lt": cursor_datetime})
            | (Q(**{f"{sort_field}": cursor_datetime}) & Q(id__lt=cursor_object_id))
        )
    else:
        # For asc: next page has newer dates OR same date with larger IDs
        return conversations_query.filter(
            Q(**{f"{sort_field}__gt": cursor_datetime})
            | (Q(**{f"{sort_field}": cursor_datetime}) & Q(id__gt=cursor_object_id))
        )


def _apply_cursor_filter(conversations_query, cursor: str, sort_field: str, sort_order: Optional[str]):
    """The query restricted to the conversations after the cursor; an invalid cursor is ignored (the query comes back
    unchanged). A date sort takes a "timestamp_objectid" cursor or a bare ObjectId, any other sort an ObjectId.
    """
    try:
        # If we're sorting by a date field, use proper date-based pagination
        if sort_field in ["last_message_date", "date"]:
            # Try to decode cursor as timestamp first (new format)
            try:
                # Format: timestamp_objectid
                if "_" in cursor:
                    cursor_timestamp_text, cursor_object_id_text = cursor.split("_", 1)
                    cursor_datetime = datetime.fromtimestamp(float(cursor_timestamp_text))
                    cursor_object_id = ObjectId(cursor_object_id_text)
                    conversations_query = _filter_by_date_cursor(
                        conversations_query, sort_field, sort_order, cursor_datetime, cursor_object_id
                    )
                else:
                    # A bare ObjectId cursor is also accepted
                    cursor_object_id = ObjectId(cursor)
                    conversations_query = _filter_by_object_id_cursor(conversations_query, cursor_object_id, sort_order)
            except (ValueError, TypeError):
                # Fall back to ObjectId-only format
                cursor_object_id = ObjectId(cursor)
                conversations_query = _filter_by_object_id_cursor(conversations_query, cursor_object_id, sort_order)
        else:
            # For non-date fields, use ObjectId pagination (existing behavior)
            cursor_object_id = ObjectId(cursor)
            conversations_query = _filter_by_object_id_cursor(conversations_query, cursor_object_id, sort_order)
    except Exception:
        # Invalid cursor, ignore it
        pass
    return conversations_query


def _build_next_cursor(last_conversation_doc: Conversation, sort_field: str) -> str:
    """The cursor of the next page, from the page's last conversation: "timestamp_id" when the inbox is sorted by a
    date field holding a datetime, otherwise the conversation id.
    """
    # Create cursor based on sort field for better pagination
    if sort_field in ["last_message_date", "date"]:
        # Use timestamp + ObjectId format for date-based sorting
        sort_field_value = getattr(last_conversation_doc, sort_field, None)
        if sort_field_value:
            if isinstance(sort_field_value, datetime):
                sort_timestamp = sort_field_value.timestamp()
                return f"{sort_timestamp}_{str(last_conversation_doc.id)}"
            else:
                # Fallback to ObjectId if date field is not a datetime
                return str(last_conversation_doc.id)
        else:
            return str(last_conversation_doc.id)
    else:
        # For non-date fields, use ObjectId only
        return str(last_conversation_doc.id)


def _prefetch_customers_and_messages(
    tenant_name: str, page_conversation_docs: List[Conversation], should_include_tool_calls: bool
) -> tuple[Dict[str, CustomerDocument], Dict[str, List[MessageDocument]]]:
    """Load the customers and the messages of all the page's conversations in two queries instead of one per
    conversation. Returns (customers by phone number, messages in date order by conversation id); tool messages only
    when asked for.
    """
    page_phone_numbers = list({conversation_doc.phone_number for conversation_doc in page_conversation_docs})
    page_conversation_ids = [str(conversation_doc.id) for conversation_doc in page_conversation_docs]

    # Batch-fetch all customers in one query
    page_customer_docs = CustomerDocument.objects(tenant_name=tenant_name, phone_number__in=page_phone_numbers)
    customer_by_phone = {customer_doc.phone_number: customer_doc for customer_doc in page_customer_docs}

    # Batch-fetch all messages in one query
    messages_query = MessageDocument.objects(tenant_name=tenant_name, conversation_id__in=page_conversation_ids)
    if not should_include_tool_calls:
        messages_query = messages_query.filter(role__nin=["tool", "tool_calls"])
    messages_query = messages_query.order_by("date")
    page_message_docs = list(messages_query)

    # Group messages by conversation_id
    messages_by_conversation_id: Dict[str, List] = defaultdict(list)
    for message_doc in page_message_docs:
        messages_by_conversation_id[message_doc.conversation_id].append(message_doc)
    return customer_by_phone, messages_by_conversation_id


def _serialize_conversation_page(
    page_conversation_docs: List[Conversation],
    customer_by_phone: Dict[str, CustomerDocument],
    messages_by_conversation_id: Dict[str, List[MessageDocument]],
    should_include_customer: bool,
    should_include_messages: bool,
    should_include_tool_calls: bool,
    should_include_media_url: bool,
) -> List[Dict[str, Any]]:
    """The page's conversations as API dicts built from the prefetched customers and messages (only the last message
    of each when messages are not asked for), with the customer embedded when asked for.
    """
    serialized_conversations = []
    for conversation_doc in page_conversation_docs:
        conversation_message_docs = messages_by_conversation_id.get(str(conversation_doc.id), [])
        if not should_include_messages and conversation_message_docs:
            # Only keep the last message
            conversation_message_docs = [conversation_message_docs[-1]]
        if should_include_customer:
            serialize_conversation = conversation_doc.serialize_for_api_with_customer
        else:
            serialize_conversation = conversation_doc.serialize_for_api
        serialized_conversations.append(
            serialize_conversation(
                should_include_messages=should_include_messages,
                should_include_tool_calls=should_include_tool_calls,
                should_include_media_url=should_include_media_url,
                prefetched_message_docs=conversation_message_docs,
                prefetched_customer_doc=customer_by_phone.get(conversation_doc.phone_number),
            )
        )
    return serialized_conversations


# This route must stay above "/{tenant_name}/{conversation_id}": FastAPI matches routes in declaration order.
@conversations_router.get("/{tenant_name}/infinite", response_model=InfiniteScrollResponse)
async def page_conversations_by_cursor(
    tenant_name: str,
    current_user: str = Depends(authenticate_current_user),
    platform: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    phone_number: Optional[str] = None,
    conversation_status: Optional[str] = None,
    read_by_manager: Optional[bool] = None,
    last_message_date: Optional[str] = None,
    max_result: int = 20,
    cursor: Optional[str] = None,  # cursor for pagination
    include_media_url: bool = True,
    sort_by: Optional[str] = "last_message_date",
    sort_order: Optional[str] = "desc",
    include_tool_calls: Optional[bool] = False,
    include_customer: bool = True,
    query: Optional[str] = None,
    include_messages: bool = True,
):
    """
    Fetches conversations with infinite scroll pagination and customer data populated.
    Uses cursor-based pagination for better performance with large datasets.
    If query is provided, searches in message text content.
    """
    # Get query filters (without the query parameter for message search)
    query_filters = build_conversation_filter_query(
        tenant_name,
        platform,
        from_date,
        to_date,
        phone_number,
        conversation_status,
        read_by_manager,
        last_message_date,
        search_text=None,  # We'll handle query separately
    )

    sort_field, sort_direction_prefix = _resolve_sort_field(sort_by, sort_order)

    # If query is provided, search in messages first to get conversation IDs and phone numbers
    if query and query.strip():
        matching_conversation_object_ids = _find_conversation_ids_by_text(tenant_name, query.strip())
        if matching_conversation_object_ids is not None:
            query_filters &= Q(id__in=matching_conversation_object_ids)
        else:
            # No matching messages found, return empty result
            return {"conversations": [], "next_cursor": None, "has_more": False, "total_count": 0}

    # Start building the query
    conversations_query = Conversation.objects(query_filters).order_by(f"{sort_direction_prefix}{sort_field}")

    # Apply cursor-based pagination if cursor is provided
    if cursor:
        conversations_query = _apply_cursor_filter(conversations_query, cursor, sort_field, sort_order)

    # Limit results
    page_conversations_query = conversations_query.limit(max_result + 1)  # +1 to check if there are more results

    # Convert to list to check length
    page_conversation_docs = list(page_conversations_query)
    # Check if there are more results
    has_more = len(page_conversation_docs) > max_result
    if has_more:
        page_conversation_docs = page_conversation_docs[:max_result]

    # Get next cursor
    next_cursor = None
    if has_more and page_conversation_docs:
        next_cursor = _build_next_cursor(page_conversation_docs[-1], sort_field)

    # Get total count of conversations matching the filter (without pagination)
    total_filtered_count = Conversation.objects(query_filters).count()

    # Convert conversations to dictionary and filter messages
    # Batch-fetch customers and messages for all conversations to avoid N+1 queries
    customer_by_phone, messages_by_conversation_id = _prefetch_customers_and_messages(
        tenant_name, page_conversation_docs, include_tool_calls
    )

    # Build result using prefetched data
    # The conversation's profile_name + phone_number are used instead
    serialized_conversations = _serialize_conversation_page(
        page_conversation_docs,
        customer_by_phone,
        messages_by_conversation_id,
        include_customer,
        include_messages,
        include_tool_calls,
        include_media_url,
    )

    # Return response with pagination info
    return {
        "conversations": serialized_conversations,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total_count": total_filtered_count,  # Total matching the filter
        "page_count": len(serialized_conversations),  # Count in current page
    }


@conversations_router.get("/{tenant_name}/{conversation_id}", response_model=Dict[str, Any])
async def fetch_conversation_with_messages(
    tenant_name: str,
    conversation_id: str,
    current_user: str = Depends(authenticate_current_user),
    include_tool_calls: bool = False,
    include_media_url: bool = True,
):
    """
    Fetches a single conversation with full details including all messages and customer data.
    """
    try:
        # Try to find by ID first
        if ObjectId.is_valid(conversation_id):
            conversation_doc = Conversation.objects(id=ObjectId(conversation_id), tenant_name=tenant_name).first()
        else:
            raise HTTPException(status_code=400, detail="Invalid conversation ID")

        if not conversation_doc:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return conversation_doc.serialize_for_api_with_customer(
            should_include_messages=True, should_include_tool_calls=include_tool_calls, should_include_media_url=include_media_url
        )
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))
