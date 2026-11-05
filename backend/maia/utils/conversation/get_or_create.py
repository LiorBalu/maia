"""Finds the open (online) conversation of a customer on a platform, or opens one."""

from typing import Optional

from maia.models.conversations import Conversation, build_online_conversation_filter


def _replace_placeholder_profile_name(
    online_conversation: Conversation, sender_id: str, customer_profile_name: Optional[str]
) -> None:
    """Give the conversation the customer's profile name when its own is empty or still the raw sender id;
    saves only when it changes the name."""
    if (
        customer_profile_name
        and customer_profile_name != online_conversation.profile_name
        and (not online_conversation.profile_name or online_conversation.profile_name == sender_id)
    ):
        # Only overwrite when currently unset or still the raw id (e.g. phone number) —
        # never clobber a real name (from the WhatsApp push name) that's already there.
        online_conversation.profile_name = customer_profile_name
        online_conversation.save()


def ensure_online_conversation(
    tenant_name: str, sender_id: str, platform: str = "unknown", customer_profile_name: Optional[str] = None
) -> Conversation:
    """
    Get or create a conversation for the given tenant and customer.

    Platforms are always kept in separate conversations: the platform is part of the lookup key so
    each platform (WhatsApp, telephone, ...) keeps its own online conversation for a number.

    :param tenant_name: The name of the tenant.
    :param sender_id: The ID of the customer.
    :param platform: The platform of the conversation.
    :param customer_profile_name: The profile name of the customer.
    :return: The conversation document.
    """
    online_conversation = Conversation.objects(
        **build_online_conversation_filter(tenant_name, sender_id, platform)
    ).first()

    if not online_conversation:
        online_conversation = Conversation(
            tenant_name=tenant_name, phone_number=sender_id, platform=platform, profile_name=customer_profile_name
        )
        online_conversation.save()
    else:
        _replace_placeholder_profile_name(online_conversation, sender_id, customer_profile_name)

    return online_conversation
