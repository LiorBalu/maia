"""Finds the open (online) conversation of a customer on a platform, or opens one."""

from typing import Optional

from maia.models.conversations import Conversation, online_conversation_filter


def get_or_create_online_conversation(
    tenant_name: str, customer_id: str, platform: str = "unknown", profile_name: Optional[str] = None
) -> Conversation:
    """
    Get or create a conversation for the given tenant and customer.

    Platforms are always kept in separate conversations: the platform is part of the lookup key so
    each platform (WhatsApp, telephone, ...) keeps its own online conversation for a number.

    :param tenant_name: The name of the tenant.
    :param customer_id: The ID of the customer.
    :param platform: The platform of the conversation.
    :param profile_name: The profile name of the customer.
    :return: The conversation document.
    """
    conversation = Conversation.objects(**online_conversation_filter(tenant_name, customer_id, platform)).first()

    if not conversation:
        conversation = Conversation(
            tenant_name=tenant_name, phone_number=customer_id, platform=platform, profile_name=profile_name
        )
        conversation.save()
    elif (
        profile_name
        and profile_name != conversation.profile_name
        and (not conversation.profile_name or conversation.profile_name == customer_id)
    ):
        # Only overwrite when currently unset or still the raw id (e.g. phone number) —
        # never clobber a real name (from the WhatsApp push name) that's already there.
        conversation.profile_name = profile_name
        conversation.save()

    return conversation
