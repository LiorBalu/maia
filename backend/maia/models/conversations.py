"""A conversation = one customer on one platform in one tenant; online/offline status, SSE broadcast of updates."""

import logging
from datetime import datetime, timezone

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


def _get_redis_broadcast_client():
    """
    Singleton Redis client for SSE broadcasting.
    Reuses a single connection instead of creating one per Conversation.save().
    """
    if not hasattr(_get_redis_broadcast_client, "_client") or _get_redis_broadcast_client._client is None:
        from maia.services.redis_clients import get_sync_redis

        _get_redis_broadcast_client._client = get_sync_redis(decode_responses=False)
    return _get_redis_broadcast_client._client


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
        result = super(Conversation, self).save(*args, **kwargs)

        # Broadcast conversation status change directly via Redis Pub/Sub (no Celery needed)
        try:
            self._broadcast_status_change_via_redis()
        except Exception as e:
            logger.error(f"Failed to broadcast SSE via Redis: {str(e)}")

        return result

    def _broadcast_status_change_via_redis(self):
        """
        Broadcast conversation status change directly via Redis Pub/Sub.
        Publishes to the 'sse:broadcast' channel — the same channel that
        redis_sse_manager listens on — so SSE clients receive the update
        without needing a Celery worker.
        Uses a singleton Redis connection (_get_redis_broadcast_client).
        """
        import json as _json

        r = _get_redis_broadcast_client()

        # Reload from DB to get the freshest data
        conversation = Conversation.objects(id=self.id).first()
        if not conversation:
            return

        # Build conversation dict (same logic as sse_routes._safe_to_dict)
        # Use lightweight dict without messages for SSE broadcast
        if hasattr(conversation, "to_dict_with_customer"):
            conversation_dict = conversation.to_dict_with_customer(include_messages=True)
        elif hasattr(conversation, "to_dict"):
            conversation_dict = conversation.to_dict(include_messages=True)
        else:
            conversation_dict = {"id": str(conversation.id), "tenant_name": conversation.tenant_name}

        update_data = {
            "type": "message_update",
            "conversation": conversation_dict,
            "message": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status_change": True,
        }

        broadcast_data = {
            "tenant_name": conversation.tenant_name,
            "data": update_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sender_instance": "direct_sync",
        }

        json_data = _json.dumps(broadcast_data, default=str)
        r.publish("sse:broadcast", json_data)

    def to_dict(
        self,
        include_messages=True,
        include_tool_calls=False,
        include_media_url=True,
        prefetched_messages=None,
        prefetched_customer=None,
    ):
        """Conversation as a dict for the API: messages (prefetched, or the newest 500; tool calls optional),
        serialized meta_data and a profile name resolved from the customer or the last user message.
        """
        from maia.models.messages import MessageDocument
        from bson import ObjectId

        def serialize_value(value):
            """Recursively serialize ObjectId values to strings"""
            if isinstance(value, ObjectId):
                return str(value)
            elif isinstance(value, dict):
                return {k: serialize_value(v) for k, v in value.items()}
            elif isinstance(value, datetime):
                return value.isoformat()
            elif isinstance(value, list):
                return [serialize_value(item) for item in value]
            else:
                return value

        if prefetched_messages is not None:
            # Use pre-fetched messages (already filtered and sorted)
            all_messages = prefetched_messages
        else:
            query = MessageDocument.objects(
                tenant_name=self.tenant_name, phone_number=self.phone_number, conversation_id=str(self.id)
            )

            if not include_tool_calls:
                query = query.filter(role__nin=["tool", "tool_calls"])

            if include_messages:
                all_messages = list(query.order_by("-date").limit(500))[::-1]
            else:
                # Get only the last message
                all_messages = query.order_by("-date").limit(1)

        # Safely convert messages to dict, handling any potential issues
        messages = []
        for message in all_messages:
            if message and hasattr(message, "to_dict"):
                try:
                    msg_dict = message.to_dict()
                    if not include_media_url and "media_url" in msg_dict:
                        del msg_dict["media_url"]
                    messages.append(msg_dict)
                except AttributeError:
                    # Skip messages that don't have a proper to_dict method
                    continue
        # If profile_name not set on conversation, try to get from customer name
        profile_name = self.profile_name
        if not profile_name:
            # Use prefetched customer if available
            cust = prefetched_customer
            if cust is None:
                from maia.models.customers import CustomerDocument as CustDoc

                cust = CustDoc.objects(tenant_name=self.tenant_name, phone_number=self.phone_number).only("name").first()
            if cust and cust.name:
                profile_name = cust.name
        if not profile_name and messages:
            # Fallback: get profile_name from last user message
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("profile_name"):
                    profile_name = msg["profile_name"]
                    break

        return {
            "obj_id": str(self.id),
            "tenant_name": self.tenant_name,
            "platform": self.platform,
            "date": self.date,
            "phone_number": self.phone_number,
            "conversation_status": self.conversation_status,
            "read_by_manager": self.read_by_manager,
            "last_message_date": self.last_message_date,
            "messages": messages,
            "meta_data": serialize_value(self.meta_data),
            "profile_name": profile_name,
        }

    def to_dict_with_customer(
        self,
        include_messages=True,
        include_tool_calls=False,
        include_media_url=True,
        prefetched_messages=None,
        prefetched_customer=None,
    ):
        """Return conversation dict with populated customer data"""
        from datetime import datetime

        from bson import ObjectId

        from maia.models.customers import CustomerDocument

        def serialize_value(value):
            """Recursively serialize ObjectId values to strings"""
            if isinstance(value, ObjectId):
                return str(value)
            elif isinstance(value, datetime):
                return value.isoformat()
            elif isinstance(value, dict):
                return {k: serialize_value(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [serialize_value(item) for item in value]
            else:
                return value

        # Get customer data (use prefetched if available)
        customer = prefetched_customer
        if customer is None:
            customer = CustomerDocument.objects(tenant_name=self.tenant_name, phone_number=self.phone_number).first()

        customer_data = None
        if customer:
            # Serialize customer meetings
            meetings = []
            if customer.meetings:
                for meeting in customer.meetings:
                    meetings.append(
                        {
                            "meeting_id": meeting.meeting_id,
                            "meeting_url": meeting.meeting_url,
                            "start_time": meeting.start_time,
                            "end_time": meeting.end_time,
                            "status": meeting.status,
                        }
                    )

            customer_data = {
                "id": str(customer.id),
                "name": customer.name,
                "phone_number": customer.phone_number,
                "email": customer.email,
                "platform": customer.platform,
                "status": customer.status,
                "reply_mode": customer.reply_mode,
                "avatar": customer.avatar,
                "created_date": customer.created_date,
                "updated_date": customer.updated_date,
                "meta_data": serialize_value(customer.meta_data),
                "meetings": meetings,
            }

        # Get base conversation dict (which is already serialized)
        conv_dict = self.to_dict(
            include_messages=include_messages,
            include_tool_calls=include_tool_calls,
            include_media_url=include_media_url,
            prefetched_messages=prefetched_messages,
            prefetched_customer=customer,
        )

        # Add customer data
        conv_dict["customer"] = serialize_value(customer_data)

        return conv_dict

    def finish_conversation(self):
        """Mark the conversation ended and attach its messages (including legacy ones with conversation_id=online) to
        its id.
        """
        from maia.models.messages import MessageDocument
        self.conversation_status = "ended"
        for message in self.messages:
            if isinstance(message, MessageDocument):
                message.conversation_id = str(self.id)
                message.save()
        msgs = MessageDocument.objects(
            conversation_id="online", phone_number=self.phone_number, tenant_name=self.tenant_name
        )
        for msg in msgs:
            msg.conversation_id = str(self.id)
            msg.save()
        self.save()


def online_conversation_filter(tenant_name: str, phone_number: str, platform: str | None = None) -> dict:
    """Build the lookup filter for the online conversation of a (tenant, number).

    Platforms are always kept in separate conversations: `platform` is added to the key whenever it
    is provided. Pass platform=None (the default) for callers that intentionally want any online
    conversation regardless of platform.
    """
    query = {"tenant_name": tenant_name, "phone_number": phone_number, "conversation_status": "online"}
    if platform is not None:
        query["platform"] = platform
    return query
