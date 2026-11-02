"""The reply pipeline: cooldown guard, MessageContext, tenant hooks, the RAG answer (raised as ReplyReady), splitting and sending it."""

import json
import logging
from datetime import datetime, timedelta

from tenants.hooks import run_tenant_hooks
from maia.observability.task_context import contextual_task
from maia.pipeline.context import MessageContext, ReplyReady
from maia.inbound.save.common import ensure_thread_id_in_conversation, retrieve_human_delay_setting
from maia.services.registry import get_sender_service
from maia.models.agents import AgentDocument
from maia.models.customers import fetch_or_create_customer
from maia.models.conversations import Conversation, online_conversation_filter
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument
from maia.utils.conversation.get_or_create import get_or_create_online_conversation
from maia.utils.convert_media_url_to_base64 import convert_media_to_langchain_format
from maia.utils.split_message_by_sentences import split_message_by_sentences

logger = logging.getLogger(__name__)

# Longest text sent as one message; longer answers are split on sentence boundaries.
MAX_MESSAGE_LENGTH = 10000


@contextual_task
async def reply_to_message(
    tenant_name: str,
    user_id: str,
    agent_config: dict = None,
    task_message_id: str = None,
    conversation_id: str = None,
) -> str:
    """Generate and send the bot's reply for a saved message (the heart of the pipeline).

    Order: cooldown guard (in a burst only the last task answers), customer + MessageContext,
    generation (``_generate_answer``: tenant hook, RAG; a ``ReplyReady`` raised by a hook or a tool
    short-circuits it with a fixed text), then the send phase: guards, web = save only, other
    platforms = ``_send_answer``. Returns a short status text, or the JSON {message, tool_call}.
    """
    tenant = TenantDocument.objects(name=tenant_name).first()
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)

    last_messages = get_conversation_messages(tenant_name, user_id, conversation_id)
    last_user_message = last_messages[-1] if last_messages else None
    if not last_user_message:
        logger.warning("Message not found for task_message_id=%s", task_message_id)
        return "Message not found"

    if _in_cooldown(last_user_message, chat_settings):
        # In a burst (e.g. 3 messages in 1 s with a 10 s delay) last_user_message is always the most
        # recent one, but task_message_id points to the exact message that triggered *this* task.
        task_message = MessageDocument.objects(id=task_message_id).first() if task_message_id else None
        _try_save_task_message_to_redis(tenant, tenant_name, user_id, task_message or last_user_message, conversation_id)
        logger.info("⏳ Message still in cooldown period, skipping AI response")
        return "Message is still in the cooldown period"

    customer = fetch_or_create_customer(
        number_phone=user_id,
        tenant_name=tenant_name,
        profile_name=last_user_message.profile_name,
        platform=last_user_message.platform,
    )
    args = build_message_context(
        tenant_name,
        user_id,
        tenant,
        chat_settings,
        last_user_message,
        customer,
        conversation_id=conversation_id,
    )
    log_user_response_details(last_user_message, label="💬 USER MESSAGE CONTENT:")

    session_id = _resolve_session_id(tenant_name, user_id, last_user_message, conversation_id)
    try:
        answer = await _generate_answer(args, last_user_message, agent_config, session_id)
    except ReplyReady as e:
        answer = str(e)

    if not answer.strip():
        logger.warning("⚠️ AI returned empty response, not sending to user")
        return "No response generated"
    if has_new_user_message_before_response(
        last_user_message, chat_settings
    ) and not have_tool_message_after_last_user_message(last_user_message):
        logger.info("⏭️ New user message received before AI response was sent. Skipping AI response.")
        return "New user message detected - no response generated"
    if args["platform"] == "web":
        save_AI_message(args, answer)
        logger.info("💬 AI response saved for platform=%s (no WhatsApp send needed)", args["platform"])
        return _web_result(args, answer)

    _send_answer(args, answer)
    return _web_result(args, answer)


def _in_cooldown(last_user_message: MessageDocument, chat_settings: ChatSettingsDocument) -> bool:
    """True while the human-like delay since the last user message has not elapsed (chat platforms only)."""
    if last_user_message.platform in ["web", "email"]:
        return False
    delay = timedelta(seconds=retrieve_human_delay_setting(chat_settings))
    return last_user_message.date + delay > datetime.utcnow()


def _resolve_session_id(tenant_name: str, user_id: str, last_user_message: MessageDocument, conversation_id: str = None) -> str:
    """The Redis history key: the online conversation id (shared history for the user)."""
    if conversation_id:
        return conversation_id
    conversation_doc = Conversation.objects(
        **online_conversation_filter(tenant_name, user_id, last_user_message.platform)
    ).first()
    return str(conversation_doc.id)


def _message_media(message: MessageDocument) -> list | None:
    """The message's image/audio as a one-item LangChain media list, or None (no media / conversion failed)."""
    if not (message.media_url and message.msg_type in ["image", "audio"]):
        return None
    media_item = convert_media_to_langchain_format(url=message.media_url, media_type=message.msg_type)
    if not media_item:
        logger.warning("⚠️ Failed to convert %s", message.msg_type)
        return None
    media_icon = "📸" if message.msg_type == "image" else "🎵"
    logger.info("%s %s converted to base64", media_icon, message.msg_type.capitalize())
    return [media_item]


async def _generate_answer(args: MessageContext, last_user_message: MessageDocument, agent_config: dict, session_id: str) -> str:
    """Run the tenant hook (binds the tools) and the RAG; return the answer text ("" when the RAG failed).

    The RAG's sources / reformulated question / error details are stored on the user message for debugging.
    """
    await run_tenant_hooks(args, args["tenant_name"])

    # Generation: LangChain + Cohere RAG (imported lazily: heavy LangChain modules).
    from maia.knowledge.conversational_rag import chat_with_documents

    if agent_config is None:
        agent_doc = AgentDocument.objects(tenant_name=args["tenant_name"], is_active=True).first()
        if agent_doc:
            agent_config = agent_doc.agent_config

    res = await chat_with_documents(
        tenant_name=args["tenant_name"],
        message=last_user_message.message,
        session_id=session_id,
        agent_config=agent_config,
        decorated_function_tools=args["tool_names"],
        request=args,
        media=_message_media(last_user_message),
    )
    answer = res.get("answer") or ""
    if answer:
        last_user_message.meta_data["sources"] = res.get("sources", [])
        last_user_message.meta_data["reformulated_question"] = res.get("reformulated_question", "")
    else:
        # No answer (RAG error): nothing is sent to the user; details stay on the message for debugging.
        logger.warning(
            "⚠️ RAG returned no answer for tenant=%s (error_details=%s)", args["tenant_name"], res.get("error_details")
        )
    last_user_message.meta_data["error_details"] = res.get("error_details", "")
    last_user_message.save()
    return answer


def _send_answer(args: MessageContext, answer: str) -> None:
    """Send the answer through the platform service and save each sent part as an AI message.

    ``---`` in the answer separates messages; each is split again on sentence boundaries at
    MAX_MESSAGE_LENGTH. The thread id returned by the service (email) is kept on the AI message
    and on the conversation, so follow-ups and replies stay in the same thread.
    """
    if not args["meta_data"].get("should_reply_after_process", True):
        logger.info("🛑 should_reply_after_process=False, AI response generated but NOT sent to user")
        return

    sender_service = get_sender_service(
        tenant_name=args["tenant_name"], service_name="sender_service", platform=args["platform"], to=args["recipient_id"]
    )
    ai_message = None
    for message_str in answer.split("---"):
        for message_truncate in split_message_by_sentences(message_str, MAX_MESSAGE_LENGTH):
            res = sender_service.send_whatsapp_message(
                phone_number=args["sender_id"],
                message=message_truncate,
                thread_id=args["meta_data"].get("thread_id"),
                message_id=args["meta_data"].get("email_message_id", args["message_id"]),
                subject=args["meta_data"].get("subject", ""),
            )
            sent_meta = {}
            returned_thread_id = res.get("thread_id")
            if returned_thread_id:
                sent_meta["thread_id"] = returned_thread_id
                sent_meta["subject"] = args["meta_data"].get("subject", "")
                args["meta_data"].setdefault("thread_id", returned_thread_id)
                ensure_thread_id_in_conversation(str(args["conversation"].id), returned_thread_id)
            ai_message = save_AI_message(
                args,
                str(message_truncate),
                sid=res["sid"],
                fake_sid=args.get("fake_sid"),
                meta_data=sent_meta,
            )
            logger.info("✅ AI message sent via %s to %s (sid=%s)", args["platform"], args["sender_id"], res.get("sid"))
    if ai_message:
        log_user_response_details(ai_message, label="🤖 AI MESSAGE CONTENT:")


def _web_result(args: MessageContext, answer: str) -> str:
    """The JSON the web channel returns to the browser: the answer and the tool calls made for it."""
    return json.dumps({"message": answer, "tool_call": args.get("tool_calls_log", [])}, ensure_ascii=False)


def _try_save_task_message_to_redis(
    tenant, tenant_name: str, user_id: str, task_message, conversation_id: str = None
) -> None:
    """Save the task-triggering message to Redis history so it is not lost on early returns.

    In burst scenarios (e.g. 3 messages in 1 s with a 10 s delay), each task knows its own
    message via task_message_id. This ensures every message ends up in the LangChain Redis
    history even when reply_to_message returns early (cooldown, block AI, agent mode).
    """
    try:
        if not task_message:
            return
        if not tenant:
            return
        if task_message.meta_data.get("langchain_synced"):
            return
        from maia.knowledge.chat_history import add_message_to_history

        if conversation_id:
            session_id = conversation_id
        else:
            conv = get_or_create_online_conversation(
                tenant_name=tenant_name,
                customer_id=user_id,
                platform=task_message.platform,
                profile_name=task_message.profile_name,
            )
            session_id = str(conv.id)
        add_message_to_history(
            tenant_name=tenant_name, session_id=session_id, text=task_message.message or "", media=_message_media(task_message)
        )
        task_message.meta_data["langchain_synced"] = True
        # Use atomic $set so we don't overwrite other meta_data keys that were written
        # concurrently on a different in-memory copy of the same document
        # (e.g. meta_data keys written by other handlers).
        MessageDocument._get_collection().update_one(
            {"_id": task_message.id}, {"$set": {"meta_data.langchain_synced": True}}
        )
        logger.info("✅ Task message saved to Redis history (id=%s)", task_message.id)
    except Exception as e:
        logger.warning("⚠️ _try_save_task_message_to_redis failed (non-critical): %s", e)


def save_AI_message(args, message, sid="", fake_sid=None, meta_data: dict = None) -> MessageDocument:
    """Save the AI's message; when a message with the same sid (or fake_sid) exists, update its text instead."""
    if not message or message.strip() == "":
        return None
    meta_data = dict(meta_data or {})

    # Check if a message with this sid already exists
    if sid:
        existing_message = MessageDocument.objects(tenant_name=args["tenant_name"], sid=sid).first()

        if existing_message:
            # Update the existing message
            existing_message.message = message
            existing_message.date = datetime.now(tz=datetime.utcnow().astimezone().tzinfo)
            existing_message.save()
            return existing_message
    if fake_sid:
        existing_message = MessageDocument.objects(tenant_name=args["tenant_name"], meta_data__fake_sid=fake_sid).first()

        if existing_message:
            # Update the existing message
            existing_message.message = message
            existing_message.date = datetime.now(tz=datetime.utcnow().astimezone().tzinfo)
            existing_message.save()
            return existing_message

    if fake_sid:
        meta_data["fake_sid"] = fake_sid
    # Create new message if no existing one found
    ai_message = MessageDocument(
        tenant_name=args["tenant_name"],
        platform=args["platform"],
        phone_number=args["sender_id"],
        sender_id=str(args["customer"].id),
        profile_name=args["sender_name"],
        message=message,
        role="assistant",
        conversation_id=str(args["conversation"].id),
        to=args["recipient_id"],
        msg_type="text",
        media_url=None,
        sid=sid or fake_sid or "sid_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
        message_sids=[],
        date=datetime.now(tz=datetime.utcnow().astimezone().tzinfo),
        meta_data=meta_data,
    )
    try:
        ai_message.save()
    except Exception as e:
        logger.warning("⚠️ Failed to save AI message: %s", e)

    return ai_message


def build_message_context(
    tenant_name, user_id, tenant, chat_settings, message, customer, conversation_id=None
) -> MessageContext:
    """Build the pipeline's ``MessageContext`` for the inbound ``message`` (chat platforms)."""
    if conversation_id:
        conversation = Conversation.objects(id=conversation_id).first()
        if not conversation:
            conversation = get_or_create_online_conversation(tenant_name, user_id, message.platform, customer.name)
    else:
        conversation = get_or_create_online_conversation(tenant_name, user_id, message.platform, customer.name)
    return MessageContext(
        tenant_name=tenant_name,
        platform=message.platform,
        customer=customer,
        tenant=tenant,
        conversation=conversation,
        chat_settings=chat_settings,
        sender_id=user_id,
        recipient_id=message.to,
        sender_name=customer.name,
        message_text=message.message or "",
        message_id=message.sid,
        message_type=message.msg_type,
        media_url=message.media_url,
        media_content_type=message.meta_data.get("media_content_type"),
        meta_data={
            "request_message": message,
            "thread_id": message.meta_data.get("thread_id"),
            "email_message_id": message.meta_data.get("email_message_id"),
            "subject": message.meta_data.get("subject"),
        },
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )


def log_user_response_details(message: MessageDocument, label="💬 MESSAGE CONTENT:"):
    """Log the message's tenant, platform, sender and text (readable debug block)."""
    logger.info(f"Tenant Name:     {message.tenant_name}")
    logger.info(f"Platform:       {message.platform}")
    logger.info(f"Profile Name:   {message.profile_name}")
    logger.info(f"Phone Number:   {message.phone_number}")
    logger.info(f"Message Date:   {message.date.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(label)
    logger.info(f"{message.message}")
    if message.media_url:
        logger.info(f"\nMedia URL: {message.media_url}")
        if message.meta_data.get("media_content_type"):
            logger.info(f"Media Type: {message.meta_data.get('media_content_type')}")
    logger.info("=" * 80 + "\n")


def get_conversation_messages(tenant_name, user_id, conversation_id=None):
    """The newest 500 messages of the customer's online conversation, oldest first, without the ones flagged
    exclude_from_conversation.
    """
    if conversation_id:
        conv = Conversation.objects(id=conversation_id).first()
        if not conv:
            conv = get_or_create_online_conversation(tenant_name, user_id, "")
    else:
        conv = get_or_create_online_conversation(tenant_name, user_id, "")
    return list(
        MessageDocument.objects(
            tenant_name=tenant_name,
            phone_number=user_id,
            conversation_id=str(conv.id),
            meta_data__exclude_from_conversation__ne=True,
        )
        .order_by("-date")
        .limit(500)
    )[::-1]

def has_new_user_message_before_response(
    last_user_message: MessageDocument, chat_settings: ChatSettingsDocument
) -> bool:
    """Check if there is a new user message after the last_user_message
    Args:
        last_user_message (MessageDocument): The last user message to compare against
    """
    if last_user_message.platform == "web" or (
        chat_settings.chatbot_human_like_time
        and (chat_settings.chatbot_human_like_delay or 0) == 0
        or not chat_settings.chatbot_human_like_time
    ):
        return False
    new_messages = MessageDocument.objects(
        tenant_name=last_user_message.tenant_name,
        phone_number=last_user_message.phone_number,
        date__gt=last_user_message.date,
        role="user",
        conversation_id=last_user_message.conversation_id,
    )
    return new_messages.count() > 0


def have_tool_message_after_last_user_message(last_user_message: MessageDocument) -> bool:
    """Check if there is a tool message after the last_user_message
    Args:
        last_user_message (MessageDocument): The last user message to compare against
    """
    tool_messages = MessageDocument.objects(
        tenant_name=last_user_message.tenant_name,
        phone_number=last_user_message.phone_number,
        date__gt=last_user_message.date,
        role__in=["tool", "tool_calls"],
        conversation_id=last_user_message.conversation_id,
    )
    return tool_messages.count() > 0
