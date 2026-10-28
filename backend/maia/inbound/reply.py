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
from maia.models.conversations import Conversation
from maia.models.messages import MessageDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument
from maia.utils.conversation.get_or_create import get_or_create_online_conversation
from maia.utils.convert_media_url_to_base64 import convert_media_to_langchain_format
from maia.utils.split_message_by_sentences import split_message_by_sentences

logger = logging.getLogger(__name__)


@contextual_task
async def reply_to_message(
    tenant_name: str,
    user_id: str,
    agent_config: dict = None,
    task_message_id: str = None,
    single_message_mode: bool = False,
    conversation_id: str = None,
) -> str:
    """Generate and send the bot's reply for a saved message (the heart of the pipeline).

    Order: cooldown guard (in a burst only the last task answers), customer + MessageContext,
    tenant hook (tools), RAG generation, then the send phase inside ``except ReplyReady``:
    guards, split on ``---`` and by length, send through the platform service, save the AI
    message. Returns a short status text, or the JSON {message, tool_call} for the web channel.
    """
    tenant = TenantDocument.objects(name=tenant_name).first()

    # single_message_mode: True when a specific message_id is passed (web page-by-page).
    # In this mode the LangChain session_id is the message id itself instead of the
    # conversation id, so each page/call gets its own isolated Redis history.
    chat_settings = ChatSettingsDocument.objects(tenant_name=tenant_name).first()
    if not chat_settings:
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)

    custom_prompt = None

    if not single_message_mode:
        # Fetch the last message from the user
        last_messages = get_conversation_messages(tenant_name, user_id, conversation_id)
        last_user_message = last_messages[-1] if last_messages else None
    else:
        last_messages = []
        last_user_message = MessageDocument.objects(id=task_message_id).first()
    if not last_user_message:
        logger.warning("Message not found for task_message_id=%s", task_message_id)
        return "Message not found"
    # Determine which specific message triggered this task.
    # In burst scenarios (e.g. 3 messages in 1 s with a 10 s delay) last_user_message is always
    # the most recent one, but task_message_id points to the exact message that triggered *this* task.
    task_message = MessageDocument.objects(id=task_message_id).first() if task_message_id else None
    if task_message is None:
        task_message = last_user_message

    if (
        last_user_message.date + timedelta(seconds=retrieve_human_delay_setting(chat_settings)) > datetime.utcnow()
        and last_user_message.platform not in ["web", "email"]
        and not single_message_mode
    ):
        _try_save_task_message_to_redis(tenant, tenant_name, user_id, task_message, conversation_id)
        logger.info("⏳ Message still in cooldown period, skipping AI response")
        return "Message is still in the cooldown period"

    # Fetch or create the customer
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
        custom_prompt,
        last_user_message,
        customer,
        conversation_id=conversation_id,
    )

    # Pretty formatted logging for debug purposes
    log_user_response_details(last_user_message, label="💬 USER MESSAGE CONTENT:")
    ai_message_raw = ""
    ai_message = None
    try:

        await run_tenant_hooks(args, args["tenant_name"])

        # Generation: LangChain + Cohere RAG.
        from maia.knowledge.conversational_rag import chat_with_documents

        if agent_config is None:
            agent_doc = AgentDocument.objects(tenant_name=tenant_name, is_active=True).first()
            if agent_doc:
                agent_config = agent_doc.agent_config

        # Determine the Redis session_id:
        # - single_message_mode (message_id kwarg set): use the message id directly so
        #   each isolated page/call has its own fresh history in Redis.
        # - normal mode: use the online Conversation id (shared history for the user).
        if single_message_mode:
            session_id = str(last_user_message.id)
            logger.info(f"Single message mode: using message_id as session_id ({session_id})")
        else:
            if conversation_id:
                session_id = conversation_id
            else:
                from maia.models.conversations import online_conversation_filter

                conversation_doc = Conversation.objects(
                    **online_conversation_filter(tenant_name, user_id, last_user_message.platform)
                ).first()
                session_id = str(conversation_doc.id)

        # Collect media from the current message only
        media = None
        if last_user_message.media_url and last_user_message.msg_type in ["image", "audio"]:
            # Convert media to LangChain format
            media_item = convert_media_to_langchain_format(
                url=last_user_message.media_url, media_type=last_user_message.msg_type
            )

            if media_item:
                media = [media_item]
                media_icon = "📸" if last_user_message.msg_type == "image" else "🎵"
                logger.info("%s %s converted to base64", media_icon, last_user_message.msg_type.capitalize())
            else:
                logger.warning("⚠️ Failed to convert %s", last_user_message.msg_type)
        message_str = last_user_message.message
        qa_metadata_filter = customer.get_meta_data_field("qa_metadata_filter", None)
        rag_categories = customer.get_meta_data_field("rag_categories", None)

        res = await chat_with_documents(
            tenant_name=args["tenant_name"],
            message=message_str,
            session_id=session_id,
            agent_config=agent_config,
            decorated_function_tools=args["tool_names"],
            request=args,
            media=media,
            qa_metadata_filter=qa_metadata_filter,
            qa_categories=rag_categories,
            document_categories=rag_categories,
        )
        if res.get("answer"):
            last_user_message.meta_data["sources"] = res.get("sources", [])
            last_user_message.meta_data["reformulated_question"] = res.get("reformulated_question", "")
            last_user_message.meta_data["error_details"] = res.get("error_details", "")
            last_user_message.meta_data["usage"] = res.get("usage", {})
            last_user_message.save()
            raise ReplyReady(res["answer"])
        # No answer (RAG error): nothing is sent to the user; details stay on the message for debugging.
        last_user_message.meta_data["error_details"] = res.get("error_details", "")
        last_user_message.save()
        logger.warning("⚠️ RAG returned no answer for tenant=%s (error_details=%s)", tenant_name, res.get("error_details"))
        raise ReplyReady("")
    except ReplyReady as e:
        ai_message_raw = str(e)
        meta_data = {}
        if not ai_message_raw.strip():
            logger.warning("⚠️ AI returned empty response, not sending to user")
            return "No response generated"
        if has_new_user_message_before_response(
            last_user_message, chat_settings
        ) and not have_tool_message_after_last_user_message(last_user_message):
            logger.info("⏭️ New user message received before AI response was sent. Skipping AI response.")
            return "New user message detected - no response generated"
        if args["platform"] == "web":
            save_AI_message(args, ai_message_raw, meta_data=meta_data)
            logger.info("💬 AI response saved for platform=%s (no WhatsApp send needed)", args["platform"])
            return json.dumps(
                {"message": ai_message_raw, "tool_call": args.get("tool_calls_log", [])}, ensure_ascii=False
            )

        sender_service = get_sender_service(
            tenant_name=args["tenant_name"], service_name="sender_service", platform=args["platform"], to=args["recipient_id"]
        )

        if ai_message_raw and ai_message_raw.strip() != "":
            # split message by max length
            max_length = 10000

            # split message by "---"
            if "---" in ai_message_raw:
                messages = ai_message_raw.split("---")
            else:
                messages = [ai_message_raw]
            for message_str in messages:
                messages_truncate = split_message_by_sentences(message_str, max_length)
                should_reply = args["meta_data"].get(
                    "should_reply_after_process",
                    args["customer"].get_meta_data_field("should_reply_after_process", True),
                )
                if should_reply:
                    for message_truncate in messages_truncate:
                        res = sender_service.send_whatsapp_message(
                            phone_number=args["sender_id"],
                            message=message_truncate,
                            thread_id=args["meta_data"].get("thread_id"),
                            message_id=args["meta_data"].get("email_message_id", args["message_id"]),
                            subject=args["meta_data"].get("subject", ""),
                        )

                        # Persist thread_id returned by the send service into
                        # the AI message meta_data and on the conversation, so
                        # that both outbound follow-ups and inbound replies stay
                        # in the same thread.
                        sent_meta = dict(meta_data)
                        returned_thread_id = res.get("thread_id")
                        if returned_thread_id:
                            sent_meta["thread_id"] = returned_thread_id
                            sent_meta.setdefault("subject", args["meta_data"].get("subject", ""))
                            args["meta_data"].setdefault("thread_id", returned_thread_id)
                            ensure_thread_id_in_conversation(str(args["conversation"].id), returned_thread_id)

                        ai_message = save_AI_message(
                            args,
                            str(message_truncate),
                            sid=res["sid"],
                            fake_sid=args.get("fake_sid"),
                            meta_data=sent_meta,
                        )
                        logger.info(
                            "✅ AI message sent via %s to %s (sid=%s)", args["platform"], args["sender_id"], res.get("sid")
                        )
                else:
                    logger.info("🛑 should_reply_after_process=False, AI response generated but NOT sent to user")
        if ai_message:
            log_user_response_details(ai_message, label="🤖 AI MESSAGE CONTENT:")

    return json.dumps({"message": ai_message_raw, "tool_call": args.get("tool_calls_log", [])}, ensure_ascii=False)


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
        from maia.utils.conversation.get_or_create import get_or_create_online_conversation

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
        media = None
        if task_message.media_url and task_message.msg_type in ["image", "audio"]:
            media_item = convert_media_to_langchain_format(url=task_message.media_url, media_type=task_message.msg_type)
            if media_item:
                media = [media_item]
        add_message_to_history(
            tenant_name=tenant_name, session_id=session_id, text=task_message.message or "", media=media
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


def save_AI_message(args, message, sid="", fake_sid=None, meta_data: dict = {}) -> MessageDocument:
    """Helper function to save AI message to the database"""
    if not message or message.strip() == "":
        return None

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
        sid=sid or fake_sid or str("sid_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")),
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
    tenant_name, user_id, tenant, chat_settings, extra_prompt_name, message, customer, conversation_id=None
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
        extra_prompt_name=extra_prompt_name,
        additional_context="",
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
