"""A conversation = one customer on one platform in one tenant; online/offline status, SSE broadcast of updates."""

import json
import logging
from datetime import datetime, timezone

from bson import ObjectId
from mongoengine import (
    DO_NOTHING,
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    ListField,
    ReferenceField,
    StringField,
)

logger = logging.getLogger(__name__)


def _ensure_sse_redis_client():
    """
    Singleton Redis client for SSE broadcasting.
    Reuses a single connection instead of creating one per Conversation.save().
    """
    if not hasattr(_ensure_sse_redis_client, "_client") or _ensure_sse_redis_client._client is None:
        from maia.services.redis_clients import build_sync_redis_client

        _ensure_sse_redis_client._client = build_sync_redis_client(should_decode_responses=False)
    return _ensure_sse_redis_client._client


def _serialize_for_sse(conversation_doc):
    """The conversation payload of an SSE event: with its customer when that serializer exists, else the plain
    payload, else only the id and tenant_name.
    """
    if hasattr(conversation_doc, "serialize_for_api_with_customer"):
        return conversation_doc.serialize_for_api_with_customer(should_include_messages=True)
    if hasattr(conversation_doc, "serialize_for_api"):
        return conversation_doc.serialize_for_api(should_include_messages=True)
    return {"id": str(conversation_doc.id), "tenant_name": conversation_doc.tenant_name}


def _build_sse_envelope(tenant_name, conversation_payload):
    """The message_update event (status_change) of a conversation, wrapped in the envelope (tenant_name, data)
    that sse_broadcast_manager reads from the 'sse:broadcast' channel.
    """
    update_event = {
        "type": "message_update",
        "conversation": conversation_payload,
        "message": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status_change": True,
    }

    return {
        "tenant_name": tenant_name,
        "data": update_event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sender_instance": "direct_sync",
    }


def _serialize_json_value(value):
    """Recursively serialize ObjectId values to strings"""
    if isinstance(value, ObjectId):
        return str(value)
    elif isinstance(value, dict):
        return {key: _serialize_json_value(nested_value) for key, nested_value in value.items()}
    elif isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, list):
        return [_serialize_json_value(element) for element in value]
    else:
        return value


def _serialize_message_docs(message_docs, should_include_media_url):
    """The API payload of each message document (media_url dropped unless asked for); a document that cannot be
    serialized is skipped.
    """
    # Safely convert messages to dict, handling any potential issues
    message_payloads = []
    for message_doc in message_docs:
        if message_doc and hasattr(message_doc, "serialize_for_api"):
            try:
                message_payload = message_doc.serialize_for_api()
                if not should_include_media_url and "media_url" in message_payload:
                    del message_payload["media_url"]
                message_payloads.append(message_payload)
            except AttributeError:
                # Skip messages that don't have a proper serialize_for_api method
                continue
    return message_payloads


def _serialize_meetings(meetings):
    """One dict per embedded customer meeting (id, url, start and end time, status); empty without meetings."""
    meeting_payloads = []
    if meetings:
        for meeting in meetings:
            meeting_payloads.append(
                {
                    "meeting_id": meeting.meeting_id,
                    "meeting_url": meeting.meeting_url,
                    "start_time": meeting.start_time,
                    "end_time": meeting.end_time,
                    "status": meeting.status,
                }
            )
    return meeting_payloads


def _serialize_customer(customer_doc):
    """The customer payload of a conversation (meta_data made JSON-safe, meetings listed); None without a customer."""
    if not customer_doc:
        return None
    # Serialize customer meetings
    meeting_payloads = _serialize_meetings(customer_doc.meetings)

    return {
        "id": str(customer_doc.id),
        "name": customer_doc.name,
        "phone_number": customer_doc.phone_number,
        "email": customer_doc.email,
        "platform": customer_doc.platform,
        "status": customer_doc.status,
        "reply_mode": customer_doc.reply_mode,
        "avatar": customer_doc.avatar,
        "created_date": customer_doc.created_date,
        "updated_date": customer_doc.updated_date,
        "meta_data": _serialize_json_value(customer_doc.meta_data),
        "meetings": meeting_payloads,
    }


class Conversation(Document):
    """The thread of one customer on one platform: status (online / ended), read flag and meta_data (thread id,
    call stats, call record).
    """
    tenant_name = StringField(required=True)
    platform = StringField(required=True, default="waha")
    date = DateTimeField(default=datetime.now(timezone.utc))
    phone_number = StringField(required=True)
    messages = ListField(ReferenceField("MessageDocument", reverse_delete_rule=DO_NOTHING))
    conversation_status = StringField(default="online")
    read_by_manager = BooleanField(default=False)
    last_message_date = DateTimeField()
    profile_name = StringField(required=False, default=None)
    meta_data = DictField(required=False, default=dict)

    meta = {
        "collection": "conversations",
        "indexes": [
            {"fields": ["tenant_name", "-last_message_date"]},
            {"fields": ["tenant_name", "phone_number", "conversation_status"]},
            {"fields": ["tenant_name", "conversation_status"]},
            {"fields": ["tenant_name", "platform"]},
        ],
    }

    def save(self, *args, **kwargs):
        """Save, then broadcast the conversation over Redis Pub/Sub for the SSE clients."""
        # Save first so the DB has the latest data when SSE clients receive the notification
        saved_conversation = super(Conversation, self).save(*args, **kwargs)

        # Broadcast conversation status change directly via Redis Pub/Sub (no Celery needed)
        try:
            self._publish_sse_update()
        except Exception as error:
            logger.error(f"Failed to broadcast SSE via Redis: {str(error)}")

        return saved_conversation

    def _publish_sse_update(self):
        """
        Broadcast conversation status change directly via Redis Pub/Sub.
        Publishes to the 'sse:broadcast' channel — the same channel that
        sse_broadcast_manager listens on — so SSE clients receive the update
        without needing a Celery worker.
        Uses a singleton Redis connection (_ensure_sse_redis_client).
        """
        redis_client = _ensure_sse_redis_client()

        # Reload from DB to get the freshest data
        fresh_conversation = Conversation.objects(id=self.id).first()
        if not fresh_conversation:
            return

        conversation_payload = _serialize_for_sse(fresh_conversation)
        broadcast_envelope = _build_sse_envelope(fresh_conversation.tenant_name, conversation_payload)

        encoded_envelope = json.dumps(broadcast_envelope, default=str)
        redis_client.publish("sse:broadcast", encoded_envelope)

    def _load_message_docs(self, should_include_messages, should_include_tool_calls):
        """This conversation's messages (tool calls left out unless asked for): the newest 500 in chronological
        order, or a query of the last one only.
        """
        from maia.models.messages import MessageDocument

        message_query = MessageDocument.objects(
            tenant_name=self.tenant_name, phone_number=self.phone_number, conversation_id=str(self.id)
        )

        if not should_include_tool_calls:
            message_query = message_query.filter(role__nin=["tool", "tool_calls"])

        if should_include_messages:
            return list(message_query.order_by("-date").limit(500))[::-1]
        # Get only the last message
        return message_query.order_by("-date").limit(1)

    def _resolve_profile_name(self, prefetched_customer_doc, message_payloads):
        """The display name: the conversation's profile_name, else the customer's name (prefetched or loaded), else
        the profile_name of the last user message.
        """
        # If profile_name not set on conversation, try to get from customer name
        resolved_profile_name = self.profile_name
        if not resolved_profile_name:
            # Use prefetched customer if available
            customer_doc = prefetched_customer_doc
            if customer_doc is None:
                from maia.models.customers import CustomerDocument

                customer_doc = (
                    CustomerDocument.objects(tenant_name=self.tenant_name, phone_number=self.phone_number)
                    .only("name")
                    .first()
                )
            if customer_doc and customer_doc.name:
                resolved_profile_name = customer_doc.name
        if not resolved_profile_name and message_payloads:
            # Fallback: get profile_name from last user message
            for message_payload in reversed(message_payloads):
                if (
                    isinstance(message_payload, dict)
                    and message_payload.get("role") == "user"
                    and message_payload.get("profile_name")
                ):
                    resolved_profile_name = message_payload["profile_name"]
                    break
        return resolved_profile_name

    def serialize_for_api(
        self,
        should_include_messages=True,
        should_include_tool_calls=False,
        should_include_media_url=True,
        prefetched_message_docs=None,
        prefetched_customer_doc=None,
    ):
        """Conversation as a dict for the API: messages (prefetched, or the newest 500; tool calls optional),
        serialized meta_data and a profile name resolved from the customer or the last user message.
        """
        if prefetched_message_docs is not None:
            # Use pre-fetched messages (already filtered and sorted)
            message_docs = prefetched_message_docs
        else:
            message_docs = self._load_message_docs(should_include_messages, should_include_tool_calls)

        message_payloads = _serialize_message_docs(message_docs, should_include_media_url)
        resolved_profile_name = self._resolve_profile_name(prefetched_customer_doc, message_payloads)

        return {
            "obj_id": str(self.id),
            "tenant_name": self.tenant_name,
            "platform": self.platform,
            "date": self.date,
            "phone_number": self.phone_number,
            "conversation_status": self.conversation_status,
            "read_by_manager": self.read_by_manager,
            "last_message_date": self.last_message_date,
            "messages": message_payloads,
            "meta_data": _serialize_json_value(self.meta_data),
            "profile_name": resolved_profile_name,
        }

    def serialize_for_api_with_customer(
        self,
        should_include_messages=True,
        should_include_tool_calls=False,
        should_include_media_url=True,
        prefetched_message_docs=None,
        prefetched_customer_doc=None,
    ):
        """Return conversation dict with populated customer data"""
        from maia.models.customers import CustomerDocument

        # Get customer data (use prefetched if available)
        customer_doc = prefetched_customer_doc
        if customer_doc is None:
            customer_doc = CustomerDocument.objects(
                tenant_name=self.tenant_name, phone_number=self.phone_number
            ).first()

        customer_payload = _serialize_customer(customer_doc)

        # Get base conversation dict (which is already serialized)
        conversation_payload = self.serialize_for_api(
            should_include_messages=should_include_messages,
            should_include_tool_calls=should_include_tool_calls,
            should_include_media_url=should_include_media_url,
            prefetched_message_docs=prefetched_message_docs,
            prefetched_customer_doc=customer_doc,
        )

        # Add customer data
        conversation_payload["customer"] = _serialize_json_value(customer_payload)

        return conversation_payload

    def _attach_legacy_online_messages(self):
        """Re-point the legacy messages still stored with conversation_id="online" (same tenant and number) to this
        conversation's id, saving them one by one.
        """
        from maia.models.messages import MessageDocument

        legacy_message_docs = MessageDocument.objects(
            conversation_id="online", phone_number=self.phone_number, tenant_name=self.tenant_name
        )
        for legacy_message_doc in legacy_message_docs:
            legacy_message_doc.conversation_id = str(self.id)
            legacy_message_doc.save()

    def end_and_attach_messages(self):
        """Mark the conversation ended and attach its messages (including legacy ones with conversation_id=online) to
        its id.
        """
        from maia.models.messages import MessageDocument
        self.conversation_status = "ended"
        for message_doc in self.messages:
            if isinstance(message_doc, MessageDocument):
                message_doc.conversation_id = str(self.id)
                message_doc.save()
        self._attach_legacy_online_messages()
        self.save()


def build_online_conversation_filter(tenant_name: str, phone_number: str, platform: str | None = None) -> dict:
    """Build the lookup filter for the online conversation of a (tenant, number).

    Platforms are always kept in separate conversations: `platform` is added to the key whenever it
    is provided. Pass platform=None (the default) for callers that intentionally want any online
    conversation regardless of platform.
    """
    lookup_filter = {"tenant_name": tenant_name, "phone_number": phone_number, "conversation_status": "online"}
    if platform is not None:
        lookup_filter["platform"] = platform
    return lookup_filter
