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


from maia.models.conversations import Conversation, build_online_conversation_filter

logger = logging.getLogger(__name__)


def _serialize_api_value(value):
    """Recursively serialize ObjectId values to strings"""
    from bson import ObjectId

    if isinstance(value, ObjectId):
        return str(value)
    elif isinstance(value, dict):
        return {key: _serialize_api_value(nested_value) for key, nested_value in value.items()}
    elif isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, list):
        return [_serialize_api_value(element) for element in value]
    else:
        return value


def _collect_dated_messages(conversation):
    """The messages referenced by the conversation that carry a date; a broken reference is logged and skipped."""
    dated_messages = []
    for message_index, referenced_message in enumerate(conversation.messages):
        try:
            # Try to access the date attribute - this will fail if the reference is broken
            if (
                referenced_message is not None
                and hasattr(referenced_message, "date")
                and referenced_message.date is not None
            ):
                dated_messages.append(referenced_message)
        except Exception as reference_error:
            logger.error(f"Warning: Skipping broken message reference at index {message_index} in conversation {conversation.id}: {type(reference_error).__name__}: {reference_error}")
            continue
    return dated_messages


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

    def _assign_display_name(self):
        """Set profile_name in memory (nothing is saved): when empty, 'user' for a user message, 'agent' for a
        manager message, 'AI' otherwise; always 'AI' for an assistant message.
        """
        if not self.profile_name:
            if self.role == "user":
                self.profile_name = "user"
            elif self.role == "manager":
                self.profile_name = "agent"
            else:
                self.profile_name = "AI"
        if self.role == "assistant":
            self.profile_name = "AI"

    def serialize_for_api(self):
        """Message as a dict for the API, with a display name per role."""
        self._assign_display_name()

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
            "meta_data": _serialize_api_value(self.meta_data),
        }

    def _find_conversation_by_id(self):
        """The conversation whose id is conversation_id; None when that id is not an ObjectId (legacy "online") or
        the lookup fails.
        """
        conversation = None
        try:
            from bson import ObjectId

            if ObjectId.is_valid(self.conversation_id):
                conversation = Conversation.objects(id=self.conversation_id).first()
        except Exception:
            pass
        return conversation

    def _ensure_online_conversation(self):
        """The customer's online conversation of this message's platform, created and saved when missing."""
        conversation = Conversation.objects(
            **build_online_conversation_filter(self.tenant_name, self.phone_number, self.platform)
        ).first()
        if not conversation:
            conversation = Conversation(
                tenant_name=self.tenant_name,
                phone_number=self.phone_number,
                platform=self.platform,
                date=datetime.now(timezone.utc),
                conversation_status="online",
            )
            conversation.save()
        return conversation

    def _refresh_last_message_date(self, conversation):
        """Set the conversation's last_message_date to the date of its latest dated message; this message's date
        when none has a date or on an error.
        """
        try:
            # Sort messages by date and get the latest message date
            # Build dated_messages list carefully to handle potential broken references
            dated_messages = _collect_dated_messages(conversation)

            if dated_messages:
                conversation.last_message_date = sorted(
                    dated_messages,
                    key=lambda message_doc: message_doc.date.replace(tzinfo=None) if message_doc.date else datetime.min,
                )[-1].date
            else:
                conversation.last_message_date = self.date
        except Exception as error:
            logger.error(f"Error sorting messages for conversation {conversation.id if conversation else 'unknown'}: {type(error).__name__}: {error}")
            conversation.last_message_date = self.date

    def _update_read_by_manager_flag(self, conversation):
        """A customer message marks the conversation unread for the manager, a manager message marks it read."""
        # Reset read_by_manager to False when a new customer message arrives
        if self.role == "user":
            conversation.read_by_manager = False
        # Set read_by_manager to True when manager sends a message
        elif self.role == "manager":
            conversation.read_by_manager = True

    def _append_to_conversation(self, conversation):
        """Reference this stored message from the conversation, refresh its last message date and read flag, then
        save the conversation (which broadcasts it over SSE) and reload it.
        """
        conversation.messages.append(self)
        self._refresh_last_message_date(conversation)
        self._update_read_by_manager_flag(conversation)

        conversation.save()  # the conversation save broadcasts the update over SSE

        conversation.reload()

    def save(self, *args, **kwargs):
        """Attach the message to the customer's online conversation of its platform (created when missing), then
        update the conversation: last message date, unread on a user message, read on a manager message.
        """
        self.date = self.date if self.date else datetime.now(timezone.utc)

        if self.conversation_id:
            conversation = self._find_conversation_by_id()
        else:
            conversation = self._ensure_online_conversation()
            self.conversation_id = str(conversation.id)

        saved_message = super(MessageDocument, self).save(*args, **kwargs)


        if conversation and saved_message not in conversation.messages:
            self._append_to_conversation(conversation)
        return self
