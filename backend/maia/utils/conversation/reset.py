"""Ends a conversation: closes it."""

import logging

from maia.models.conversations import Conversation, build_online_conversation_filter

logger = logging.getLogger(__name__)


def _find_conversation_to_end(tenant_name: str, sender_id: str, conversation_id: str = None, platform: str = None):
    """The conversation to close: by id, or the online one of the number (scoped to the platform when given)."""
    if conversation_id:
        return Conversation.objects(id=conversation_id).first()
    # Platforms are always kept in separate conversations: `platform` narrows the lookup, so a
    # call reset never clears a sibling platform's (e.g. WhatsApp) online conversation.
    return Conversation.objects(**build_online_conversation_filter(tenant_name, sender_id, platform)).first()


async def _end_conversation_if_found(tenant_name: str, sender_id: str, conversation_to_end) -> None:
    """Close the conversation."""
    if not conversation_to_end:
        return

    conversation_to_end.end_and_attach_messages()


async def end_customer_conversation(tenant_name: str, sender_id: str, conversation_id: str = None, platform: str = None):
    """End a conversation: close it."""
    conversation_to_end = _find_conversation_to_end(tenant_name, sender_id, conversation_id, platform)
    await _end_conversation_if_found(tenant_name, sender_id, conversation_to_end)
    logger.info(f"Customer {sender_id} conversation reset.")
