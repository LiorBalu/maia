"""The reply pipeline: cooldown guard, MessageContext, tenant hooks, the RAG answer (raised as ReplyReady), splitting and sending it."""

import json
import logging
from datetime import datetime, timedelta

from tenants.hooks import prepare_context_for_tenant
from maia.observability.task_context import bind_log_context_from_arguments
from maia.pipeline.context import MessageContext, ReplyReady
from maia.inbound.save.common import persist_conversation_thread_id, resolve_human_like_delay_seconds
from maia.services.registry import build_platform_sender_service
from maia.models.agents import AgentDocument
from maia.models.customers import ensure_customer
from maia.models.conversations import Conversation, build_online_conversation_filter
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument
from maia.utils.conversation.get_or_create import ensure_online_conversation
from maia.utils.convert_media_url_to_base64 import build_media_content_block
from maia.utils.split_message_by_sentences import split_message_into_segments

logger = logging.getLogger(__name__)

# Longest text sent as one message; longer answers are split on sentence boundaries.
MAX_REPLY_SEGMENT_LENGTH = 10000


def _record_cooldown_message(
    tenant_doc: TenantDocument | None,
    tenant_name: str,
    sender_id: str,
    latest_customer_message: MessageDocument,
    triggering_message_id: str | None,
    conversation_id: str | None,
) -> None:
    """Cooldown branch of ``run_reply_pipeline``: record the message that triggered this task in the Redis history
    (``_record_triggering_message_in_history``), then log that the reply is skipped.
    """
    # In a burst (e.g. 3 messages in 1 s with a 10 s delay) latest_customer_message is always the most
    # recent one, but triggering_message_id points to the exact message that triggered *this* task.
    triggering_message_doc = (
        MessageDocument.objects(id=triggering_message_id).first() if triggering_message_id else None
    )
    _record_triggering_message_in_history(
        tenant_doc, tenant_name, sender_id, triggering_message_doc or latest_customer_message, conversation_id
    )
    logger.info("⏳ Message still in cooldown period, skipping AI response")


def _prepare_message_context(
    tenant_name: str,
    sender_id: str,
    tenant_doc: TenantDocument | None,
    chat_settings: ChatSettingsDocument,
    latest_customer_message: MessageDocument,
    conversation_id: str | None,
) -> MessageContext:
    """Ensure the sender's customer record, build the MessageContext and log the incoming message."""
    customer_record = ensure_customer(
        phone_or_email=sender_id,
        tenant_name=tenant_name,
        customer_profile_name=latest_customer_message.profile_name,
        platform=latest_customer_message.platform,
    )
    message_context = build_chat_message_context(
        tenant_name,
        sender_id,
        tenant_doc,
        chat_settings,
        latest_customer_message,
        customer_record,
        conversation_id=conversation_id,
    )
    log_message_details(latest_customer_message, content_label="💬 USER MESSAGE CONTENT:")
    return message_context


def _skip_reason_before_send(
    answer_text: str, latest_customer_message: MessageDocument, chat_settings: ChatSettingsDocument
) -> str | None:
    """The send-phase guards: the status text to return instead of sending, or None when the answer goes out.

    An empty answer is not sent, nor one overtaken by a newer customer message (unless a tool message came since).
    """
    if not answer_text.strip():
        logger.warning("⚠️ AI returned empty response, not sending to user")
        return "No response generated"
    if is_superseded_by_newer_customer_message(
        latest_customer_message, chat_settings
    ) and not has_newer_tool_message(latest_customer_message):
        logger.info("⏭️ New user message received before AI response was sent. Skipping AI response.")
        return "New user message detected - no response generated"
    return None


def _deliver_or_record_reply(message_context: MessageContext, answer_text: str) -> str:
    """Send phase: web = save the answer as the AI message only, other platforms = ``_deliver_answer_segments``.

    Returns the reply JSON {message, tool_call} in both cases.
    """
    if message_context["platform"] == "web":
        record_assistant_message(message_context, answer_text)
        logger.info("💬 AI response saved for platform=%s (no WhatsApp send needed)", message_context["platform"])
        return _render_reply_json(message_context, answer_text)

    _deliver_answer_segments(message_context, answer_text)
    return _render_reply_json(message_context, answer_text)


@bind_log_context_from_arguments
async def run_reply_pipeline(
    tenant_name: str,
    user_id: str,
    agent_config_override: dict = None,
    triggering_message_id: str = None,
    conversation_id: str = None,
) -> str:
    """Generate and send the bot's reply for a saved message (the heart of the pipeline).

    Order: cooldown guard (in a burst only the last task answers), customer + MessageContext,
    generation (``_answer_with_rag``: tenant hook, RAG; a ``ReplyReady`` raised by a hook or a tool
    short-circuits it with a fixed text), then the send phase: guards, web = save only, other
    platforms = ``_deliver_answer_segments``. Returns a short status text, or the JSON {message, tool_call}.
    """
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)

    recent_messages = load_recent_conversation_messages(tenant_name, user_id, conversation_id)
    latest_customer_message = recent_messages[-1] if recent_messages else None
    if not latest_customer_message:
        logger.warning("Message not found for triggering_message_id=%s", triggering_message_id)
        return "Message not found"

    if _is_in_cooldown(latest_customer_message, chat_settings):
        _record_cooldown_message(
            tenant_doc, tenant_name, user_id, latest_customer_message, triggering_message_id, conversation_id
        )
        return "Message is still in the cooldown period"

    message_context = _prepare_message_context(
        tenant_name, user_id, tenant_doc, chat_settings, latest_customer_message, conversation_id
    )

    history_session_id = _resolve_history_session_id(tenant_name, user_id, latest_customer_message, conversation_id)
    try:
        answer_text = await _answer_with_rag(
            message_context, latest_customer_message, agent_config_override, history_session_id
        )
    except ReplyReady as reply_ready:
        answer_text = str(reply_ready)

    if skip_reason := _skip_reason_before_send(answer_text, latest_customer_message, chat_settings):
        return skip_reason
    return _deliver_or_record_reply(message_context, answer_text)


def _is_in_cooldown(latest_customer_message: MessageDocument, chat_settings: ChatSettingsDocument) -> bool:
    """True while the human-like delay since the last user message has not elapsed (chat platforms only)."""
    if latest_customer_message.platform in ["web", "email"]:
        return False
    human_like_delay = timedelta(seconds=resolve_human_like_delay_seconds(chat_settings))
    return latest_customer_message.date + human_like_delay > datetime.utcnow()


def _resolve_history_session_id(tenant_name: str, sender_id: str, latest_customer_message: MessageDocument, conversation_id: str = None) -> str:
    """The Redis history key: the online conversation id (shared history for the user).

    Lookup only: the online conversation must already exist (``_ensure_history_session_id`` get-or-creates it).
    """
    if conversation_id:
        return conversation_id
    conversation_doc = Conversation.objects(
        **build_online_conversation_filter(tenant_name, sender_id, latest_customer_message.platform)
    ).first()
    return str(conversation_doc.id)


def _prepare_langchain_media(message_doc: MessageDocument) -> list | None:
    """The message's image/audio as a one-item LangChain media list, or None (no media / conversion failed)."""
    if not (message_doc.media_url and message_doc.msg_type in ["image", "audio"]):
        return None
    media_content_block = build_media_content_block(media_url=message_doc.media_url, media_kind=message_doc.msg_type)
    if not media_content_block:
        logger.warning("⚠️ Failed to convert %s", message_doc.msg_type)
        return None
    media_icon = "📸" if message_doc.msg_type == "image" else "🎵"
    logger.info("%s %s converted to base64", media_icon, message_doc.msg_type.capitalize())
    return [media_content_block]


def _resolve_agent_config(tenant_name: str, agent_config_override: dict | None) -> dict | None:
    """The agent config for the RAG: the override when given, else the tenant's active agent's config, else None."""
    if agent_config_override is not None:
        return agent_config_override
    active_agent_doc = AgentDocument.objects(tenant_name=tenant_name, is_active=True).first()
    return active_agent_doc.agent_config_for_rag if active_agent_doc else None


def _record_rag_outcome(latest_customer_message: MessageDocument, rag_result: dict, tenant_name: str) -> str:
    """Store the RAG's sources / reformulated question (with an answer) and error details on the customer message,
    save it and return the answer text ("" when the RAG failed).
    """
    answer_text = rag_result.get("answer") or ""
    if answer_text:
        latest_customer_message.meta_data["sources"] = rag_result.get("sources", [])
        latest_customer_message.meta_data["reformulated_question"] = rag_result.get("reformulated_question", "")
    else:
        # No answer (RAG error): nothing is sent to the user; details stay on the message for debugging.
        logger.warning(
            "⚠️ RAG returned no answer for tenant=%s (error_details=%s)", tenant_name, rag_result.get("error_details")
        )
    latest_customer_message.meta_data["error_details"] = rag_result.get("error_details", "")
    latest_customer_message.save()
    return answer_text


async def _answer_with_rag(message_context: MessageContext, latest_customer_message: MessageDocument, agent_config_override: dict, history_session_id: str) -> str:
    """Run the tenant hook (binds the tools) and the RAG; return the answer text ("" when the RAG failed).

    The RAG's sources / reformulated question / error details are stored on the user message for debugging.
    A ``ReplyReady`` raised by the hook or by a tool propagates to the caller and skips that recording.
    """
    await prepare_context_for_tenant(message_context, message_context["tenant_name"])

    # Generation: LangChain + Cohere RAG (imported lazily: heavy LangChain modules).
    from maia.knowledge.conversational_rag import generate_rag_answer

    agent_config = _resolve_agent_config(message_context["tenant_name"], agent_config_override)
    rag_result = await generate_rag_answer(
        tenant_name=message_context["tenant_name"],
        customer_message_text=latest_customer_message.message,
        history_session_id=history_session_id,
        agent_config=agent_config,
        tool_names=message_context["tool_names"],
        message_context=message_context,
        media_items=_prepare_langchain_media(latest_customer_message),
    )
    return _record_rag_outcome(latest_customer_message, rag_result, message_context["tenant_name"])


def _remember_reply_thread(message_context: MessageContext, returned_thread_id: str | None) -> dict:
    """The meta_data of the sent AI message: the thread id the service returned (email) and the subject.

    The thread id is also set on the context's meta_data (setdefault) and stored on the conversation; without a
    returned thread id nothing happens and the dict is empty.
    """
    reply_meta_data = {}
    if returned_thread_id:
        reply_meta_data["thread_id"] = returned_thread_id
        reply_meta_data["subject"] = message_context["meta_data"].get("subject", "")
        message_context["meta_data"].setdefault("thread_id", returned_thread_id)
        persist_conversation_thread_id(str(message_context["conversation"].id), returned_thread_id)
    return reply_meta_data


def _deliver_answer_segment(
    message_context: MessageContext, sender_service, segment_text: str
) -> MessageDocument | None:
    """Send one segment of the answer, keep the returned thread id, save the segment as an AI message, log the send.

    Returns what ``record_assistant_message`` returns: the AI message, or None for an empty segment.
    """
    send_result = sender_service.deliver_message_to_customer(
        recipient_address=message_context["sender_id"],
        message_text=segment_text,
        thread_id=message_context["meta_data"].get("thread_id"),
        message_id=message_context["meta_data"].get("email_message_id", message_context["message_id"]),
        subject=message_context["meta_data"].get("subject", ""),
    )
    reply_meta_data = _remember_reply_thread(message_context, send_result.get("thread_id"))
    ai_message_doc = record_assistant_message(
        message_context,
        str(segment_text),
        message_sid=send_result["sid"],
        echo_sid=message_context.get("fake_sid"),
        reply_meta_data=reply_meta_data,
    )
    logger.info("✅ AI message sent via %s to %s (sid=%s)", message_context["platform"], message_context["sender_id"], send_result.get("sid"))
    return ai_message_doc


def _deliver_answer_segments(message_context: MessageContext, answer_text: str) -> None:
    """Send the answer through the platform service and save each sent part as an AI message.

    ``---`` in the answer separates messages; each is split again on sentence boundaries at
    MAX_REPLY_SEGMENT_LENGTH. The thread id returned by the service (email) is kept on the AI message
    and on the conversation, so follow-ups and replies stay in the same thread.
    """
    if not message_context["meta_data"].get("should_reply_after_process", True):
        logger.info("🛑 should_reply_after_process=False, AI response generated but NOT sent to user")
        return

    sender_service = build_platform_sender_service(
        tenant_name=message_context["tenant_name"], service_name="sender_service", platform=message_context["platform"], to=message_context["recipient_id"]
    )
    last_ai_message = None
    for answer_message_text in answer_text.split("---"):
        for segment_text in split_message_into_segments(answer_message_text, MAX_REPLY_SEGMENT_LENGTH):
            last_ai_message = _deliver_answer_segment(message_context, sender_service, segment_text)
    if last_ai_message:
        log_message_details(last_ai_message, content_label="🤖 AI MESSAGE CONTENT:")


def _render_reply_json(message_context: MessageContext, answer_text: str) -> str:
    """The JSON the web channel returns to the browser: the answer and the tool calls made for it."""
    return json.dumps({"message": answer_text, "tool_call": message_context.get("tool_calls_log", [])}, ensure_ascii=False)


def _ensure_history_session_id(
    tenant_name: str, sender_id: str, triggering_message_doc: MessageDocument, conversation_id: str | None
) -> str:
    """The Redis history key for the triggering message: conversation_id when given, else the id of the sender's
    online conversation on the message's platform, created when missing (get-or-create, unlike the lookup-only
    ``_resolve_history_session_id``).
    """
    if conversation_id:
        return conversation_id
    conversation_doc = ensure_online_conversation(
        tenant_name=tenant_name,
        sender_id=sender_id,
        platform=triggering_message_doc.platform,
        customer_profile_name=triggering_message_doc.profile_name,
    )
    return str(conversation_doc.id)


def _mark_message_synced_to_history(triggering_message_doc: MessageDocument) -> None:
    """Flag the message as recorded in the Redis history (meta_data.langchain_synced), in memory and in MongoDB."""
    triggering_message_doc.meta_data["langchain_synced"] = True
    # Use atomic $set so we don't overwrite other meta_data keys that were written
    # concurrently on a different in-memory copy of the same document
    # (e.g. meta_data keys written by other handlers).
    MessageDocument._get_collection().update_one(
        {"_id": triggering_message_doc.id}, {"$set": {"meta_data.langchain_synced": True}}
    )


def _record_triggering_message_in_history(
    tenant_doc, tenant_name: str, sender_id: str, triggering_message_doc, conversation_id: str = None
) -> None:
    """Save the task-triggering message to Redis history so it is not lost on early returns.

    In burst scenarios (e.g. 3 messages in 1 s with a 10 s delay), each task knows its own
    message via triggering_message_id. This ensures every message ends up in the LangChain Redis
    history even when run_reply_pipeline returns early (cooldown, block AI, agent mode).
    """
    try:
        if not triggering_message_doc:
            return
        if not tenant_doc:
            return
        if triggering_message_doc.meta_data.get("langchain_synced"):
            return
        from maia.knowledge.chat_history import record_customer_message_in_history

        history_session_id = _ensure_history_session_id(tenant_name, sender_id, triggering_message_doc, conversation_id)
        record_customer_message_in_history(
            tenant_name=tenant_name, history_session_id=history_session_id, customer_message_text=triggering_message_doc.message or "", media_items=_prepare_langchain_media(triggering_message_doc)
        )
        _mark_message_synced_to_history(triggering_message_doc)
        logger.info("✅ Task message saved to Redis history (id=%s)", triggering_message_doc.id)
    except Exception as error:
        logger.warning("⚠️ _record_triggering_message_in_history failed (non-critical): %s", error)


def _find_sent_ai_message(tenant_name: str, message_sid: str, echo_sid: str | None) -> MessageDocument | None:
    """The tenant's message already saved with this sid, else the one whose meta_data.fake_sid is echo_sid (each
    lookup only when its id is given); None when neither matches.
    """
    if message_sid:
        existing_message_doc = MessageDocument.objects(tenant_name=tenant_name, sid=message_sid).first()
        if existing_message_doc:
            return existing_message_doc
    if echo_sid:
        existing_message_doc = MessageDocument.objects(tenant_name=tenant_name, meta_data__fake_sid=echo_sid).first()
        if existing_message_doc:
            return existing_message_doc
    return None


def _overwrite_message_text(existing_message_doc: MessageDocument, reply_text: str) -> MessageDocument:
    """Replace the saved message's text with reply_text, re-date it to now (local time zone), save and return it."""
    # Update the existing message
    existing_message_doc.message = reply_text
    existing_message_doc.date = datetime.now(tz=datetime.utcnow().astimezone().tzinfo)
    existing_message_doc.save()
    return existing_message_doc


def _build_ai_message_doc(
    message_context: MessageContext, reply_text: str, message_sid: str, echo_sid: str | None, reply_meta_data: dict
) -> MessageDocument:
    """The new, unsaved assistant MessageDocument for reply_text. Its sid is message_sid, else echo_sid, else a
    generated ``sid_<timestamp>``; a given echo_sid is also written into reply_meta_data (key fake_sid).
    """
    if echo_sid:
        reply_meta_data["fake_sid"] = echo_sid
    # Create new message if no existing one found
    return MessageDocument(
        tenant_name=message_context["tenant_name"],
        platform=message_context["platform"],
        phone_number=message_context["sender_id"],
        sender_id=str(message_context["customer"].id),
        profile_name=message_context["sender_name"],
        message=reply_text,
        role="assistant",
        conversation_id=str(message_context["conversation"].id),
        to=message_context["recipient_id"],
        msg_type="text",
        media_url=None,
        sid=message_sid or echo_sid or "sid_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
        message_sids=[],
        date=datetime.now(tz=datetime.utcnow().astimezone().tzinfo),
        meta_data=reply_meta_data,
    )


def record_assistant_message(message_context, reply_text, message_sid="", echo_sid=None, reply_meta_data: dict = None) -> MessageDocument | None:
    """Save the AI's message; when a message with the same sid (or fake_sid) exists, update its text instead."""
    if not reply_text or reply_text.strip() == "":
        return None
    reply_meta_data = dict(reply_meta_data or {})

    # Check if a message with this sid already exists
    existing_message_doc = _find_sent_ai_message(message_context["tenant_name"], message_sid, echo_sid)
    if existing_message_doc:
        return _overwrite_message_text(existing_message_doc, reply_text)

    ai_message_doc = _build_ai_message_doc(message_context, reply_text, message_sid, echo_sid, reply_meta_data)
    try:
        ai_message_doc.save()
    except Exception as error:
        logger.warning("⚠️ Failed to save AI message: %s", error)

    return ai_message_doc


def _ensure_conversation_doc(
    tenant_name: str,
    sender_id: str,
    platform: str,
    customer_profile_name: str | None = None,
    conversation_id: str | None = None,
) -> Conversation:
    """The conversation with conversation_id when given and found, else the sender's online conversation on
    platform (get-or-create through ``ensure_online_conversation``, with customer_profile_name).
    """
    if conversation_id:
        conversation_doc = Conversation.objects(id=conversation_id).first()
        if conversation_doc:
            return conversation_doc
    return ensure_online_conversation(tenant_name, sender_id, platform, customer_profile_name)


def build_chat_message_context(
    tenant_name, sender_id, tenant_doc, chat_settings, latest_customer_message, customer, conversation_id=None
) -> MessageContext:
    """Build the pipeline's ``MessageContext`` for the inbound ``latest_customer_message`` (chat platforms)."""
    conversation_doc = _ensure_conversation_doc(
        tenant_name, sender_id, latest_customer_message.platform, customer.name, conversation_id=conversation_id
    )
    return MessageContext(
        tenant_name=tenant_name,
        platform=latest_customer_message.platform,
        customer=customer,
        tenant=tenant_doc,
        conversation=conversation_doc,
        chat_settings=chat_settings,
        sender_id=sender_id,
        recipient_id=latest_customer_message.to,
        sender_name=customer.name,
        message_text=latest_customer_message.message or "",
        message_id=latest_customer_message.sid,
        message_type=latest_customer_message.msg_type,
        media_url=latest_customer_message.media_url,
        media_content_type=latest_customer_message.meta_data.get("media_content_type"),
        meta_data={
            "request_message": latest_customer_message,
            "thread_id": latest_customer_message.meta_data.get("thread_id"),
            "email_message_id": latest_customer_message.meta_data.get("email_message_id"),
            "subject": latest_customer_message.meta_data.get("subject"),
        },
        tool_names=[],
        tool_schemas=[],
        tool_results={},
    )


def log_message_details(message_doc: MessageDocument, content_label="💬 MESSAGE CONTENT:"):
    """Log the message's tenant, platform, sender and text (readable debug block)."""
    logger.info(f"Tenant Name:     {message_doc.tenant_name}")
    logger.info(f"Platform:       {message_doc.platform}")
    logger.info(f"Profile Name:   {message_doc.profile_name}")
    logger.info(f"Phone Number:   {message_doc.phone_number}")
    logger.info(f"Message Date:   {message_doc.date.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(content_label)
    logger.info(f"{message_doc.message}")
    if message_doc.media_url:
        logger.info(f"\nMedia URL: {message_doc.media_url}")
        if message_doc.meta_data.get("media_content_type"):
            logger.info(f"Media Type: {message_doc.meta_data.get('media_content_type')}")
    logger.info("=" * 80 + "\n")


def load_recent_conversation_messages(tenant_name, sender_id, conversation_id=None):
    """The newest 500 messages of the customer's online conversation, oldest first, without the ones flagged
    exclude_from_conversation.
    """
    conversation_doc = _ensure_conversation_doc(tenant_name, sender_id, "", conversation_id=conversation_id)
    return list(
        MessageDocument.objects(
            tenant_name=tenant_name,
            phone_number=sender_id,
            conversation_id=str(conversation_doc.id),
            meta_data__exclude_from_conversation__ne=True,
        )
        .order_by("-date")
        .limit(500)
    )[::-1]


def _is_burst_guard_off(latest_customer_message: MessageDocument, chat_settings: ChatSettingsDocument) -> bool:
    """True when newer customer messages are not checked: on web, with a delay time but the delay switch off
    (False / None), or without a delay time (0 / None).
    """
    return latest_customer_message.platform == "web" or (
        chat_settings.chatbot_human_like_time
        and (chat_settings.chatbot_human_like_delay or 0) == 0
        or not chat_settings.chatbot_human_like_time
    )


def is_superseded_by_newer_customer_message(
    latest_customer_message: MessageDocument, chat_settings: ChatSettingsDocument
) -> bool:
    """Check if there is a new user message after the latest_customer_message
    Args:
        latest_customer_message (MessageDocument): The last user message to compare against
    """
    if _is_burst_guard_off(latest_customer_message, chat_settings):
        return False
    newer_customer_messages = MessageDocument.objects(
        tenant_name=latest_customer_message.tenant_name,
        phone_number=latest_customer_message.phone_number,
        date__gt=latest_customer_message.date,
        role="user",
        conversation_id=latest_customer_message.conversation_id,
    )
    return newer_customer_messages.count() > 0


def has_newer_tool_message(latest_customer_message: MessageDocument) -> bool:
    """Check if there is a tool message after the latest_customer_message
    Args:
        latest_customer_message (MessageDocument): The last user message to compare against
    """
    newer_tool_messages = MessageDocument.objects(
        tenant_name=latest_customer_message.tenant_name,
        phone_number=latest_customer_message.phone_number,
        date__gt=latest_customer_message.date,
        role__in=["tool", "tool_calls"],
        conversation_id=latest_customer_message.conversation_id,
    )
    return newer_tool_messages.count() > 0
