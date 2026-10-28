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


def generate_tool_response(request, tool_call):
    """OpenAI-style tool message ({role: tool, content, tool_call_id}) from the stored result of the call."""
    return {
        "role": "tool",
        "content": [{"type": "text", "text": request["tool_results"][tool_call.id]}],
        "tool_call_id": tool_call.id,
    }


def save_tool_response(request, tool_chat_response):
    """Save the tool's response as a role=tool message of the conversation."""
    MessageDocument(
        phone_number=request["sender_id"],
        tenant_name=request["tenant_name"],
        role="tool",
        message=json.dumps(tool_chat_response, ensure_ascii=False),
        conversation_id=str(request["conversation"].id),
        date=datetime.now(timezone.utc),
        platform=request["platform"],
        meta_data={
            "tool_call_id": tool_chat_response["tool_call_id"],
            "tool_call_name": tool_chat_response["content"][0]["text"],
            "tool_call_type": "function",
            "tool_call_arguments": tool_chat_response["content"][0]["text"],
        },
    ).save()


def save_tool_call_payload(request, tool_call_payload):
    """Save the assistant's tool request as a role=tool_calls message of the conversation."""
    MessageDocument(
        phone_number=request["sender_id"],
        tenant_name=request["tenant_name"],
        role="tool_calls",
        message=json.dumps(tool_call_payload, ensure_ascii=False),
        conversation_id=str(request["conversation"].id),
        date=datetime.now(timezone.utc),
        platform=request["platform"],
    ).save()


def generate_tool_call_payload(tool_call):
    """OpenAI-style assistant message carrying one tool call (id, name, arguments)."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
            }
        ],
    }
