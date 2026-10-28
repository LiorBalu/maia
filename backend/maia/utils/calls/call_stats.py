"""Per-conversation call counters kept in `Conversation.meta_data`: answered / unanswered / hung-up counts and the last call duration and status."""

from datetime import datetime, timezone

from maia.models.conversations import Conversation


def _get_conversation(tenant_name: str, phone_number: str):
    """The customer's online telephone conversation, or the latest telephone one."""
    from maia.models.conversations import online_conversation_filter

    # Call stats belong to the telephone conversation; when the tenant splits platforms, target it
    # explicitly so phone counters don't land on the WhatsApp/text conversation.
    online = online_conversation_filter(tenant_name, phone_number, platform="telephone")
    fallback = {k: v for k, v in online.items() if k != "conversation_status"}
    return (
        Conversation.objects(**online).first()
        or Conversation.objects(**fallback).order_by("-last_message_date").first()
    )


def record_call_finished(
    tenant_name: str,
    phone_number: str,
    call_status: str,
    duration_seconds: int,
    answered: bool,
    hung_up_by_customer: bool,
) -> None:
    """Update answered / hangup / duration counters when a call ends."""
    conversation = _get_conversation(tenant_name, phone_number)
    if not conversation:
        return
    meta = dict(conversation.meta_data or {})
    if answered:
        meta["answered_count"] = int(meta.get("answered_count", 0)) + 1
    else:
        meta["unanswered_count"] = int(meta.get("unanswered_count", 0)) + 1
    if hung_up_by_customer:
        meta["hangup_by_customer_count"] = int(meta.get("hangup_by_customer_count", 0)) + 1
    meta["last_call_duration"] = int(duration_seconds or 0)
    meta["last_call_status"] = call_status
    meta["last_call_ended_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conversation.meta_data = meta
    conversation.save()
