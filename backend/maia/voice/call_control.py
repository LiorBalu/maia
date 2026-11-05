"""Call-control primitives for the realtime phone channel.

Tool calls made by the realtime model are executed through the decorated ``@llm_tool`` registry
(``execute_tool_calls_and_record_results``); the call/response pair is persisted in the conversation like any
other tool call. ``EndCall`` is raised by the ``hangup_call`` tool to end the call.
"""

import logging
import json

from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

from maia.pipeline.tool_calls import execute_tool_calls_and_record_results
from maia.pipeline.tool_messages import (
    build_assistant_tool_call_message,
    build_tool_response_message,
    record_tool_call_in_conversation,
    record_tool_response_in_conversation,
)
from maia.pipeline.context import MessageContext
from maia.voice.logger import SimpleLogger

logger = logging.getLogger(__name__)


class EndCall(Exception):
    """Raised to end the call immediately (the ``hangup_call`` tool)."""


def _build_tool_call(tool_call_id: str, tool_name: str, tool_arguments: dict) -> ChatCompletionMessageToolCall:
    """Adapt the Realtime function call to the chat-completions tool call the pipeline helpers expect."""
    return ChatCompletionMessageToolCall(
        id=tool_call_id, type="function", function=Function(name=tool_name, arguments=json.dumps(tool_arguments))
    )


def _ensure_tool_result(message_context: MessageContext, tool_call_id: str, tool_name: str) -> str:
    """Return the stored result of the tool call, after storing an "Unknown tool" error result when the registry
    left none (``message_context["tool_results"]`` is updated in place).
    """
    # A tool name the registry does not know leaves no response; tell the model instead of crashing the stream.
    message_context.setdefault("tool_results", {})
    if tool_call_id not in message_context["tool_results"]:
        message_context["tool_results"][tool_call_id] = json.dumps(
            {"status": "error", "message": f"Unknown tool: {tool_name}"}, ensure_ascii=False
        )
    return message_context["tool_results"][tool_call_id]


async def _deliver_tool_output(
    realtime_websocket, call_logger: SimpleLogger, tool_call_id: str, tool_name: str, tool_output: str
):
    """Send the tool output to the Realtime session as a function_call_output item, then ask the model for a new
    response.
    """
    tool_output_event = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": tool_call_id,
            "output": json.dumps(tool_output),
        },
    }
    call_logger.record_call_event("tool", f"Sending {tool_name} result to AI", True)
    await realtime_websocket.send(json.dumps(tool_output_event))
    # Create new response after function call
    await realtime_websocket.send(json.dumps({"type": "response.create"}))


async def execute_realtime_tool_call(tool_name, tool_arguments, realtime_websocket, twilio_stream_id, call_logger, twilio_websocket, tool_call_id, message_context):
    """Run a tool the Realtime model called: hangup_call ends the call (EndCall); other tools go through the registry,
    their call and response are saved to the conversation, the output is sent back to OpenAI and a new response is
    requested.
    """
    # Handle hangup_call immediately without processing other tools
    if tool_name == "hangup_call":
        call_logger.record_call_event("tool", "📞 Call hangup requested - immediate disconnect", True)
        raise EndCall("Call hangup requested by user")

    requested_tool_call = _build_tool_call(tool_call_id, tool_name, tool_arguments)
    record_tool_call_in_conversation(message_context, build_assistant_tool_call_message(requested_tool_call))

    await execute_tool_calls_and_record_results(message_context, [requested_tool_call])

    tool_output = _ensure_tool_result(message_context, tool_call_id, tool_name)
    record_tool_response_in_conversation(message_context, build_tool_response_message(message_context, requested_tool_call))

    await _deliver_tool_output(realtime_websocket, call_logger, tool_call_id, tool_name, tool_output)
