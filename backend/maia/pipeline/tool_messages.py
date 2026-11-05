"""Persist a tool call into the conversation transcript (used by the LangChain tool wrapper and the phone channel):

- one ``role="tool_calls"`` message: the assistant requesting the tool;
- one ``role="tool"`` message: the tool's response.
"""

import logging
import json
from datetime import datetime, timezone

from maia.models.messages import MessageDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-conversation tool call persistence
# ---------------------------------------------------------------------------


def build_tool_response_message(message_context, requested_tool_call):
    """OpenAI-style tool message ({role: tool, content, tool_call_id}) from the stored result of the call."""
    return {
        "role": "tool",
        "content": [{"type": "text", "text": message_context["tool_results"][requested_tool_call.id]}],
        "tool_call_id": requested_tool_call.id,
    }


def _persist_conversation_message(message_context, role, payload, meta_data=None):
    """Save one message of the conversation: the given role, the payload as JSON text and its meta_data (None
    leaves the field's default, an empty dict).
    """
    MessageDocument(
        phone_number=message_context["sender_id"],
        tenant_name=message_context["tenant_name"],
        role=role,
        message=json.dumps(payload, ensure_ascii=False),
        conversation_id=str(message_context["conversation"].id),
        date=datetime.now(timezone.utc),
        platform=message_context["platform"],
        meta_data=meta_data,
    ).save()


def record_tool_response_in_conversation(message_context, tool_response_message):
    """Save the tool's response as a role=tool message of the conversation."""
    _persist_conversation_message(
        message_context,
        "tool",
        tool_response_message,
        meta_data={
            "tool_call_id": tool_response_message["tool_call_id"],
            "tool_call_name": tool_response_message["content"][0]["text"],
            "tool_call_type": "function",
            "tool_call_arguments": tool_response_message["content"][0]["text"],
        },
    )


def record_tool_call_in_conversation(message_context, assistant_tool_call_message):
    """Save the assistant's tool request as a role=tool_calls message of the conversation."""
    _persist_conversation_message(message_context, "tool_calls", assistant_tool_call_message)


def build_assistant_tool_call_message(requested_tool_call):
    """OpenAI-style assistant message carrying one tool call (id, name, arguments)."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": requested_tool_call.id,
                "type": "function",
                "function": {"name": requested_tool_call.function.name, "arguments": requested_tool_call.function.arguments},
            }
        ],
    }
