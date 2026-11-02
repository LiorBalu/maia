"""Ends a conversation: closes it."""

import logging

from maia.models.conversations import Conversation, online_conversation_filter

logger = logging.getLogger(__name__)


def _get_reset_conversation(tenant_name: str, phone_number: str, conversation_id: str = None, platform: str = None):
    """The conversation to close: by id, or the online one of the number (scoped to the platform when given)."""
    if conversation_id:
        return Conversation.objects(id=conversation_id).first()
    # Platforms are always kept in separate conversations: `platform` narrows the lookup, so a
    # call reset never clears a sibling platform's (e.g. WhatsApp) online conversation.
    return Conversation.objects(**online_conversation_filter(tenant_name, phone_number, platform)).first()


async def _finish_conversation_reset(tenant_name: str, phone_number: str, conversation_doc) -> None:
    """Close the conversation."""
    if not conversation_doc:
        return

    conversation_doc.finish_conversation()


async def reset_conversation(tenant_name: str, phone_number: str, conversation_id: str = None, platform: str = None):
    """End a conversation: close it."""
    conversation_doc = _get_reset_conversation(tenant_name, phone_number, conversation_id, platform)
    await _finish_conversation_reset(tenant_name, phone_number, conversation_doc)
    logger.info(f"Customer {phone_number} conversation reset.")
