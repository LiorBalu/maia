"""Call-control primitives for the realtime phone channel.

Tool calls made by the realtime model are executed through the decorated ``@gpt_tool`` registry
(``run_tool_calls``); the call/response pair is persisted in the conversation like any other tool
call. ``EndCall`` and ``CallManager`` are raised by the tool handlers to end or hand off the call.
"""

import logging
import json

from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

from maia.pipeline.tool_calls import run_tool_calls
from maia.pipeline.tool_messages import (
    generate_tool_call_payload,
    generate_tool_response,
    save_tool_call_payload,
    save_tool_response,
)

logger = logging.getLogger(__name__)


class EndCall(Exception):
    """Raised to end the call immediately (the ``hangup_call`` tool)."""


class CallManager(Exception):
    """Raised after a ``call_to_manager`` tool call so the stream handler can run its handoff logic."""


async def handle_function_called(function_name, arguments, openai_ws, stream_sid, logger, websocket, call_id, request):
    # Handle hangup_call immediately without processing other tools
    """Run a tool the Realtime model called: hangup_call ends the call (EndCall); other tools go through the registry,
    their call and response are saved to the conversation, the output is sent back to OpenAI and a new response is
    requested; call_to_manager then raises CallManager.
    """
    if function_name == "hangup_call":
        logger.log_event("tool", "📞 Call hangup requested - immediate disconnect", True)
        raise EndCall("Call hangup requested by user")

    tool_call = ChatCompletionMessageToolCall(
        id=call_id, type="function", function=Function(name=function_name, arguments=json.dumps(arguments))
    )
    save_tool_call_payload(request, generate_tool_call_payload(tool_call))

    await run_tool_calls(request, [tool_call])

    # A tool name the registry does not know leaves no response; tell the model instead of crashing the stream.
    request.setdefault("tool_results", {})
    if call_id not in request["tool_results"]:
        request["tool_results"][call_id] = json.dumps(
            {"status": "error", "message": f"Unknown tool: {function_name}"}, ensure_ascii=False
        )

    save_tool_response(request, generate_tool_response(request, tool_call))

    func_result = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(request["tool_results"][call_id]),
        },
    }
    logger.info("Sending information to AI")
    await openai_ws.send(json.dumps(func_result))
    # Create new response after function call
    await openai_ws.send(json.dumps({"type": "response.create"}))

    if function_name == "call_to_manager":
        raise CallManager(f"Function '{function_name}' called with arguments: {arguments}")
