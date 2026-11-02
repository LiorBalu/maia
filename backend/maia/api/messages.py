"""Admin API: conversations and messages of a tenant: inbox lists (infinite scroll), send as manager, end a conversation."""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from mongoengine import Q
from pydantic import BaseModel

from maia.services.registry import get_sender_service
from maia.models.customers import CustomerDocument
from maia.models.conversations import Conversation
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.users import User, UserDocument
from maia.api.security import authenticate_user_token
from maia.utils.conversation.reset import reset_conversation
from maia.utils.conversations_filters import filter_conversations
from maia.utils.google_cloud_storage.utils.save_fastapi_file_in_bucket import save_fastapi_file_in_bucket

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
async def read_conversation(
    tenant_name: str, conversation_id: str, phone_number: str, user: User = Depends(authenticate_user_token)
):
    """Mark a conversation as read by the manager (the save broadcasts the change over SSE)."""
    conversation_doc = Conversation.objects(tenant_name=tenant_name, id=conversation_id).first()
    if not conversation_doc:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation_doc.read_by_manager = True
    conversation_doc.save()
    return JSONResponse(status_code=200, content={"message": "Conversation updated"})


def normalize_phone_number(phone_number: str) -> Optional[str]:
    """Strip the whatsapp: prefix, '+', spaces and dashes; an Israeli local number (leading 0) becomes its 972 form.
    """
    if not phone_number:
        return None
    if phone_number.startswith("whatsapp:+"):
        phone_number = phone_number.replace("whatsapp:+", "")
    phone_number = phone_number.replace("+", "").replace(" ", "").replace("-", "")
    if phone_number.startswith("0"):
        phone_number = "972" + phone_number[1:]
    return phone_number


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
async def send_message_api(
    request: Request,
    tenant_name: str,
    phone_number: str,
    background_tasks: BackgroundTasks,
    sender_number: Optional[str] = None,
    user: User = Depends(authenticate_user_token),
    platform: str = Query("waha"),
    role: str = Query("manager"),
):
    """Send a message from the dashboard to a customer (JSON, or multipart with a file); send_platform_message sends
    and saves it.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    message: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    file: Optional[UploadFile] = None

    if content_type.startswith("application/json"):
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        message = data.get("message")
        reply_to_message_id = data.get("reply_to_message_id")
        conversation_id = data.get("conversation_id")
    else:
        form = await request.form()
        message = form.get("message")
        reply_to_message_id = form.get("reply_to_message_id")
        conversation_id = form.get("conversation_id")
        form_file = form.get("file")
        if hasattr(form_file, "filename"):
            file = form_file

    if not message and file is None:
        raise HTTPException(status_code=422, detail="message or file is required")
    # await send_platform_message(
    #     tenant_name,
    #     phone_number,
    #     message,
    #     sender_number,
    #     file,
    #     user,
    #     platform)
    # Upload the file to the sender service
    # Don't normalize  IDs (IGIDs) - they are not phone numbers
    if platform != "email":
        phone_number = normalize_phone_number(phone_number)
        sender_number = normalize_phone_number(sender_number)
    media_url = None
    if file is not None:
        message_type, media_url = await save_fastapi_file_in_bucket(f"{tenant_name}/customers/{phone_number}", file)
    customer = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    if not customer:
        customer = CustomerDocument(
            name=phone_number, phone_number=phone_number, tenant_name=tenant_name, platform=platform
        )
        customer.save()
    await send_platform_message(
        tenant_name=tenant_name,
        phone_number=phone_number,
        message=message,
        sender_number=sender_number,
        media_url=media_url,
        user=user,
        platform=platform,
        role=role,
        customer=customer,
        reply_to_message_id=reply_to_message_id,
        conversation_id=conversation_id,
    )
    logger.info(f"[send_message_api] platform={platform} conversation_id={conversation_id} reply_to={reply_to_message_id}")

    return JSONResponse(status_code=200, content={"message": "Message is being sent", "phone_number": phone_number})


def format_message_for_whatsapp(message: str) -> str:
    """Turn markdown emphasis (**, __, double backticks) into WhatsApp formatting (*, _, backtick)."""
    return str(message).replace("**", "*").replace("__", "_").replace("``", "`") if message else ""


async def send_platform_message(
    tenant_name: str,
    phone_number: str,
    message: str,
    sender_number: str,
    platform: str,
    role: str,
    user: UserDocument,
    reply_to_message_id: Optional[str] = None,
    media_url: Optional[str] = None,
    customer: Optional[CustomerDocument] = None,
    conversation_id: Optional[str] = None,
):
    """Send a manager message on WhatsApp (WAHA) or email and save it: resolve the thread (reply-to message or
    conversation), send the text and the file, write the MessageDocument, add the text to the Redis history as a
    manager turn and broadcast the conversation over SSE. 424 when no sender is connected.
    """
    try:
        file_message = None
        # await asyncio.sleep(20)  # Simulate some processing delay
        sid = ""
        file_message = None
        thread_id = None
        subject = None

        # Resolve conversation_id and thread context from reply_to_message_id
        if not conversation_id and reply_to_message_id:
            reply_msg = MessageDocument.objects(sid=reply_to_message_id).first()
            if reply_msg and reply_msg.conversation_id:
                conversation_id = reply_msg.conversation_id
                if reply_msg.meta_data:
                    thread_id = reply_msg.meta_data.get("thread_id")
                    subject = reply_msg.meta_data.get("subject")
        # If conversation_id is provided, try to get thread context from the conversation
        if conversation_id and not thread_id:
            conv_obj = Conversation.objects(id=conversation_id).first()
            if conv_obj and conv_obj.meta_data:
                thread_id = conv_obj.meta_data.get("thread_id")
        # Resolve subject from the last message in the conversation if still missing
        if thread_id and not subject and conversation_id:
            last_msg = (
                MessageDocument.objects(conversation_id=conversation_id, meta_data__subject__exists=True)
                .order_by("-date")
                .first()
            )
            if last_msg and last_msg.meta_data:
                subject = last_msg.meta_data.get("subject")

        logger.info(f"[send_platform_message] platform={platform} conv={conversation_id} thread={thread_id} subject={subject}")

        final_message = message

        if platform in ["email", "waha"]:
            try:
                sender_service = get_sender_service(tenant_name, "sender_service", platform=platform, to=sender_number)
            except HTTPException:
                raise
            except Exception as sender_err:
                # No connected/usable sender for this tenant+platform (missing account, unknown
                # sender_number, bad credentials). Surface it as an actionable error instead of a bare 500.
                logger.error(f"Failed to resolve sender service for {tenant_name}/{platform}/{sender_number}: {sender_err}")
                raise HTTPException(
                    status_code=424,
                    detail=(
                        f"No connected messaging sender for tenant '{tenant_name}' on platform '{platform}'"
                        f"{f' (sender_number={sender_number})' if sender_number else ''}. "
                        f"Connect the channel before sending. Underlying error: {sender_err}"
                    ),
                )
            file_message = None

            # Send text message only if there's a message to send
            if final_message:
                send_kwargs = {"reply_to_message_id": reply_to_message_id}
                if thread_id:
                    send_kwargs["thread_id"] = thread_id
                if subject:
                    send_kwargs["subject"] = subject
                res_message = sender_service.send_whatsapp_message(
                    phone_number, message=format_message_for_whatsapp(final_message), **send_kwargs
                )
                sid = res_message["sid"]
                # Capture thread_id from send response
                if not thread_id and res_message.get("thread_id"):
                    thread_id = res_message["thread_id"]
            else:
                sid = ""
            if media_url:
                try:
                    # The sender service expects media_url as a parameter, not base64 data
                    # The service should handle downloading and encoding the file
                    res_file = sender_service.send_whatsapp_file(phone_number, message="", media_url=media_url)
                    file_message = MessageDocument(
                        profile_name=user.name,
                        date=datetime.now(timezone.utc),
                        phone_number=phone_number,
                        message="",
                        role=role,
                        sid=res_file["sid"],
                        tenant_name=tenant_name,
                        to=sender_number,
                        platform=platform,
                        media_url=media_url,
                        conversation_id=conversation_id,
                    )
                    file_message.save()
                    file_message.reload()
                except Exception as e:
                    file_message = None
                    logger.error(f"Failed to send file via {platform}: {str(e)}")
                    import traceback

                    traceback.print_exc()
        message_doc = None
        if message:
            message_doc_metadata = {}
            if reply_to_message_id:
                message_doc_metadata["reply_to_message_id"] = reply_to_message_id
            if thread_id:
                message_doc_metadata["thread_id"] = thread_id
            if subject:
                message_doc_metadata["subject"] = subject

            message_doc = MessageDocument(
                profile_name=user.name,
                date=datetime.now(timezone.utc),
                phone_number=phone_number,
                message=final_message,
                role=role,
                sid=sid,
                tenant_name=tenant_name,
                to=sender_number,
                platform=platform,
                media_url=None,
                meta_data=message_doc_metadata if message_doc_metadata else None,
                conversation_id=conversation_id,
            )
            message_doc.save()
            # Store the thread_id on the conversation for routing
            if thread_id and message_doc.conversation_id:
                from maia.inbound.save.common import ensure_thread_id_in_conversation

                ensure_thread_id_in_conversation(message_doc.conversation_id, thread_id)

        # When only a file was sent (no text message), use the file_message as message_doc
        if not message_doc and media_url and file_message:
            message_doc = file_message

        # Save the manager message into the Redis history so the AI sees it as context in the
        # next user turn.
        try:
            if final_message and role in ["manager", "agent"] and message_doc:
                tenant_doc = TenantDocument.objects(name=tenant_name).first()
                if tenant_doc:
                    online_conv = Conversation.objects(id=str(message_doc.conversation_id)).first()
                    if online_conv:
                        from maia.knowledge.chat_history import add_ai_message_to_history

                        add_ai_message_to_history(
                            tenant_name=tenant_name, session_id=str(online_conv.id), text=f"[Manager]: {final_message}"
                        )
                        if message_doc:
                            message_doc.meta_data = message_doc.meta_data or {}
                            message_doc.meta_data["langchain_synced"] = True
                            message_doc._mark_as_changed("meta_data")
                            message_doc.save()
                        logger.info(f"Manager message saved to Redis history for {phone_number}")
        except Exception as _redis_err:
            logger.error(f"Could not save manager message to Redis (non-critical): {_redis_err}")
        # The message save updated and saved the conversation, which broadcast it over SSE.

        if message_doc:
            return JSONResponse(status_code=200, content=message_doc.to_json())
        return JSONResponse(status_code=200, content={"message": "Message sent", "phone_number": phone_number})
    except HTTPException:
        raise
    except Exception as e:
        # Never let an unexpected failure surface as a bare 500 with no body.
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to send message on platform '{platform}': {e}")


@conversations_router.put("/{tenant_name}/{phone_number}/end-current-conversation")
async def end_current_conversation(tenant_name: str, phone_number: str, user: str = Depends(authenticate_user_token)):
    """Close the customer's online conversation (reset_conversation)."""
    conv = Conversation.objects(tenant_name=tenant_name, phone_number=phone_number, conversation_status="online").first()
    if not conv:
        raise HTTPException(status_code=404, detail="Online conversation not found")
    await reset_conversation(
        tenant_name=tenant_name, phone_number=phone_number, conversation_id=str(conv.id) if conv else None
    )
    return {"message": "Conversation ended"}


@conversations_router.get("/{tenant_name}/infinite", response_model=InfiniteScrollResponse)
async def get_conversations_infinite(
    tenant_name: str,
    user: str = Depends(authenticate_user_token),
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
    query_filters = filter_conversations(
        tenant_name,
        platform,
        from_date,
        to_date,
        phone_number,
        conversation_status,
        read_by_manager,
        last_message_date,
        query=None,  # We'll handle query separately
    )

    # Define allowed sorting fields
    allowed_sort_fields = ["date", "last_message_date", "phone_number", "conversation_status"]

    # Validate sorting field
    if sort_by not in allowed_sort_fields:
        sort_by = "last_message_date"

    # Determine sorting order
    sort_prefix = "-" if (sort_order or "desc").lower() == "desc" else ""

    # If query is provided, search in messages first to get conversation IDs and phone numbers
    if query and query.strip():
        # Use MongoDB text index for efficient full-text search instead of $regex
        matching_conv_ids = (
            MessageDocument.objects(tenant_name=tenant_name).search_text(query.strip()).distinct("conversation_id")
        )

        if matching_conv_ids:
            from bson import ObjectId

            conversation_object_ids = [ObjectId(cid) for cid in matching_conv_ids if cid]
            query_filters &= Q(id__in=conversation_object_ids)
        else:
            # No matching messages found, return empty result
            return {"conversations": [], "next_cursor": None, "has_more": False, "total_count": 0}

    # Start building the query
    conversations_query = Conversation.objects(query_filters).order_by(f"{sort_prefix}{sort_by}")

    # Apply cursor-based pagination if cursor is provided
    if cursor:
        try:
            from bson import ObjectId

            # If we're sorting by a date field, use proper date-based pagination
            if sort_by in ["last_message_date", "date"]:
                # Try to decode cursor as timestamp first (new format)
                try:
                    from datetime import datetime

                    # Format: timestamp_objectid
                    if "_" in cursor:
                        timestamp_str, obj_id_str = cursor.split("_", 1)
                        cursor_date = datetime.fromtimestamp(float(timestamp_str))
                        cursor_id = ObjectId(obj_id_str)

                        if sort_order == "desc":
                            # For desc: next page has older dates OR same date with smaller IDs
                            conversations_query = conversations_query.filter(
                                Q(**{f"{sort_by}__lt": cursor_date})
                                | (Q(**{f"{sort_by}": cursor_date}) & Q(id__lt=cursor_id))
                            )
                        else:
                            # For asc: next page has newer dates OR same date with larger IDs
                            conversations_query = conversations_query.filter(
                                Q(**{f"{sort_by}__gt": cursor_date})
                                | (Q(**{f"{sort_by}": cursor_date}) & Q(id__gt=cursor_id))
                            )
                    else:
                        # A bare ObjectId cursor is also accepted
                        cursor_id = ObjectId(cursor)
                        if sort_order == "desc":
                            conversations_query = conversations_query.filter(id__lt=cursor_id)
                        else:
                            conversations_query = conversations_query.filter(id__gt=cursor_id)
                except (ValueError, TypeError):
                    # Fall back to ObjectId-only format
                    cursor_id = ObjectId(cursor)
                    if sort_order == "desc":
                        conversations_query = conversations_query.filter(id__lt=cursor_id)
                    else:
                        conversations_query = conversations_query.filter(id__gt=cursor_id)
            else:
                # For non-date fields, use ObjectId pagination (existing behavior)
                cursor_id = ObjectId(cursor)
                if sort_order == "desc":
                    conversations_query = conversations_query.filter(id__lt=cursor_id)
                else:
                    conversations_query = conversations_query.filter(id__gt=cursor_id)
        except Exception:
            # Invalid cursor, ignore it
            pass

    # Limit results
    conversations = conversations_query.limit(max_result + 1)  # +1 to check if there are more results

    # Convert to list to check length
    conversations_list = list(conversations)
    # Check if there are more results
    has_more = len(conversations_list) > max_result
    if has_more:
        conversations_list = conversations_list[:max_result]

    # Get next cursor
    next_cursor = None
    if has_more and conversations_list:
        last_conv = conversations_list[-1]

        # Create cursor based on sort field for better pagination
        if sort_by in ["last_message_date", "date"]:
            # Use timestamp + ObjectId format for date-based sorting
            sort_field_value = getattr(last_conv, sort_by, None)
            if sort_field_value:
                from datetime import datetime

                if isinstance(sort_field_value, datetime):
                    timestamp = sort_field_value.timestamp()
                    next_cursor = f"{timestamp}_{str(last_conv.id)}"
                else:
                    # Fallback to ObjectId if date field is not a datetime
                    next_cursor = str(last_conv.id)
            else:
                next_cursor = str(last_conv.id)
        else:
            # For non-date fields, use ObjectId only
            next_cursor = str(last_conv.id)

    # Get total count of conversations matching the filter (without pagination)
    total_filtered_count = Conversation.objects(query_filters).count()

    # Convert conversations to dictionary and filter messages
    # Batch-fetch customers and messages for all conversations to avoid N+1 queries
    phone_numbers_batch = list({conv.phone_number for conv in conversations_list})
    conv_ids_batch = [str(conv.id) for conv in conversations_list]

    # Batch-fetch all customers in one query
    customers_batch = CustomerDocument.objects(tenant_name=tenant_name, phone_number__in=phone_numbers_batch)
    customer_map = {c.phone_number: c for c in customers_batch}

    # Batch-fetch all messages in one query
    msg_query = MessageDocument.objects(tenant_name=tenant_name, conversation_id__in=conv_ids_batch)
    if not include_tool_calls:
        msg_query = msg_query.filter(role__nin=["tool", "tool_calls"])
    msg_query = msg_query.order_by("date")
    all_messages = list(msg_query)

    # Group messages by conversation_id
    messages_by_conv: Dict[str, List] = defaultdict(list)
    for msg in all_messages:
        messages_by_conv[msg.conversation_id].append(msg)

    # Build result using prefetched data
    # The conversation's profile_name + phone_number are used instead
    if include_customer:
        result = []
        for conv in conversations_list:
            conv_id = str(conv.id)
            conv_messages = messages_by_conv.get(conv_id, [])
            if not include_messages and conv_messages:
                # Only keep the last message
                conv_messages = [conv_messages[-1]]
            result.append(
                conv.to_dict_with_customer(
                    include_messages=include_messages,
                    include_tool_calls=include_tool_calls,
                    include_media_url=include_media_url,
                    prefetched_messages=conv_messages,
                    prefetched_customer=customer_map.get(conv.phone_number),
                )
            )
    else:
        result = []
        for conv in conversations_list:
            conv_id = str(conv.id)
            conv_messages = messages_by_conv.get(conv_id, [])
            if not include_messages and conv_messages:
                conv_messages = [conv_messages[-1]]
            result.append(
                conv.to_dict(
                    include_messages=include_messages,
                    include_tool_calls=include_tool_calls,
                    include_media_url=include_media_url,
                    prefetched_messages=conv_messages,
                    prefetched_customer=customer_map.get(conv.phone_number),
                )
            )

    # Return response with pagination info
    return {
        "conversations": result,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total_count": total_filtered_count,  # Total matching the filter
        "page_count": len(result),  # Count in current page
    }


@conversations_router.get("/{tenant_name}/{conversation_id}", response_model=Dict[str, Any])
async def get_conversation_details(
    tenant_name: str,
    conversation_id: str,
    user: str = Depends(authenticate_user_token),
    include_tool_calls: bool = False,
    include_media_url: bool = True,
):
    """
    Fetches a single conversation with full details including all messages and customer data.
    """
    from bson import ObjectId

    try:
        # Try to find by ID first
        if ObjectId.is_valid(conversation_id):
            conversation = Conversation.objects(id=ObjectId(conversation_id), tenant_name=tenant_name).first()
        else:
            raise HTTPException(status_code=400, detail="Invalid conversation ID")

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return conversation.to_dict_with_customer(
            include_messages=True, include_tool_calls=include_tool_calls, include_media_url=include_media_url
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
