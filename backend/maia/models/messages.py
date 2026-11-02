"""Messages exchanged in a conversation (user, assistant, tool, manager)."""

import logging
from datetime import datetime, timezone

from mongoengine import (
    DateTimeField,
    DictField,
    Document,
    ListField,
    StringField,
)


from maia.models.conversations import Conversation, online_conversation_filter

logger = logging.getLogger(__name__)


class MessageDocument(Document):
    """One message of a conversation: role (user / assistant / manager / tool / tool_calls), text, media, the
    platform's id (sid) and meta_data (thread, sources...).
    """
    tenant_name = StringField(required=False)
    platform = StringField(required=True, default="waha")
    phone_number = StringField(required=False)
    sender_id = StringField(required=False)
    message = StringField(required=False)
    role = StringField(required=False)
    profile_name = StringField(required=False, default="")
    message_sids = ListField(StringField())
    date = DateTimeField(required=False)
    conversation_id = StringField(required=True)
    sid = StringField(unique=True, sparse=True)
    status = StringField()
    to = StringField(required=False)
    msg_type = StringField(required=False, default="text")
    media_url = StringField(required=False)
    meta_data = DictField(required=False)
    meta = {
        "collection": "messages",
        "indexes": [
            {"fields": ["tenant_name", "phone_number", "conversation_id", "date"]},
            {"fields": ["tenant_name", "conversation_id"]},
            {"fields": ["tenant_name", "phone_number", "-date"]},
            {"fields": ["tenant_name", "$message"], "default_language": "none"},
        ],
    }

    def to_dict(self):
        """Message as a dict for the API, with a display name per role."""
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

        if not self.profile_name:
            if self.role == "user":
                self.profile_name = "user"
            elif self.role == "manager":
                self.profile_name = "agent"
            else:
                self.profile_name = "AI"
        if self.role == "assistant":
            self.profile_name = "AI"

        return {
            "id": str(self.id),
            "tenant_name": self.tenant_name,
            "platform": self.platform,
            "phone_number": self.phone_number,
            "message": self.message,
            "role": self.role,
            "profile_name": self.profile_name,
            "message_sids": self.message_sids,
            "date": self.date,
            "conversation_id": self.conversation_id,
            "sid": self.sid,
            "status": self.status,
            "to": self.to,
            "msg_type": self.msg_type,
            "media_url": self.media_url,
            "meta_data": serialize_value(self.meta_data),
        }

    def save(self, *args, **kwargs):
        """Attach the message to the customer's online conversation of its platform (created when missing), then
        update the conversation: last message date, unread on a user message, read on a manager message.
        """
        self.date = self.date if self.date else datetime.now(timezone.utc)

        conv = None
        if self.conversation_id:
            try:
                from bson import ObjectId

                if ObjectId.is_valid(self.conversation_id):
                    conv = Conversation.objects(id=self.conversation_id).first()
            except Exception:
                pass
        else:
            conv = Conversation.objects(
                **online_conversation_filter(self.tenant_name, self.phone_number, self.platform)
            ).first()
            if not conv:
                conv = Conversation(
                    tenant_name=self.tenant_name,
                    phone_number=self.phone_number,
                    platform=self.platform,
                    date=datetime.now(timezone.utc),
                    conversation_status="online",
                )
                conv.save()
            self.conversation_id = str(conv.id)

        msg = super(MessageDocument, self).save(*args, **kwargs)


        if conv and msg not in conv.messages:
            conv.messages.append(self)
            try:
                # Sort messages by date and get the latest message date
                # Build valid_messages list carefully to handle potential broken references
                valid_messages = []
                for idx, m in enumerate(conv.messages):
                    try:
                        # Try to access the date attribute - this will fail if the reference is broken
                        if m is not None and hasattr(m, "date") and m.date is not None:
                            valid_messages.append(m)
                    except Exception as ref_err:
                        logger.error(f"Warning: Skipping broken message reference at index {idx} in conversation {conv.id}: {type(ref_err).__name__}: {ref_err}")
                        continue

                if valid_messages:
                    conv.last_message_date = sorted(
                        valid_messages, key=lambda msg: msg.date.replace(tzinfo=None) if msg.date else datetime.min
                    )[-1].date
                else:
                    conv.last_message_date = self.date
            except Exception as e:
                logger.error(f"Error sorting messages for conversation {conv.id if conv else 'unknown'}: {type(e).__name__}: {e}")
                conv.last_message_date = self.date
            # Reset read_by_manager to False when a new customer message arrives
            if self.role == "user":
                conv.read_by_manager = False
            # Set read_by_manager to True when manager sends a message
            elif self.role == "manager":
                conv.read_by_manager = True

            conv.save()  # the conversation save broadcasts the update over SSE

            conv.reload()
        return self
