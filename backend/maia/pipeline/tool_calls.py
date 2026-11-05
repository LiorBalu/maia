"""Runs the tool calls the model requested (Realtime phone sessions) and builds the tool schemas from the registry."""

import json
import logging
import traceback
from typing import List

from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall

from maia.pipeline.tools.function_decorator import FunctionTool, execute_tool_by_name
from maia.pipeline.context import MessageContext

logger = logging.getLogger(__name__)


def _record_tool_call_failure(
    message_context: MessageContext,
    requested_tool_call: ChatCompletionMessageToolCall,
    error_message: str,
    headline: str,
) -> None:
    """Store the error text as the call's result, then log it with the traceback of the exception being handled
    (call it from inside the ``except`` clause, so ``traceback.format_exc()`` still sees that exception).
    """
    message_context["tool_results"][requested_tool_call.id] = error_message
    logger.error(
        f"{headline}\n"
        "Function   : %s\n"
        "Tenant      : %s\n"
        "From       : %s\n"
        "Traceback  :\n%s",
        requested_tool_call.function.name,
        message_context.get("tenant_name"),
        message_context.get("sender_id"),
        traceback.format_exc(),
    )


async def _execute_single_tool_call(
    message_context: MessageContext, requested_tool_call: ChatCompletionMessageToolCall
) -> None:
    """Run one requested call of a registered tool and store its output, or the error text, under the call's id in
    ``message_context["tool_results"]``; a call of a tool the registry does not know is skipped.
    """
    tool_name = requested_tool_call.function.name

    # Check if this is a decorated function tool
    if not FunctionTool.find_tool(tool_name):
        return

    try:
        # Parse arguments
        parsed_arguments = json.loads(requested_tool_call.function.arguments)

        # Call the function
        tool_output = await execute_tool_by_name(tool_name, parsed_arguments, message_context)

        # Store the result in tool_results
        message_context["tool_results"][requested_tool_call.id] = tool_output

        logger.info(f"Decorated tool {tool_name} called successfully with result: {tool_output[:100]}...")

    except json.JSONDecodeError as decode_error:
        error_message = f"Error parsing arguments for {tool_name}: {str(decode_error)}"
        _record_tool_call_failure(
            message_context, requested_tool_call, error_message, "JSON decode error in decorated tool call"
        )

    except Exception as error:
        error_message = f"Error executing {tool_name}: {str(error)}"
        _record_tool_call_failure(
            message_context, requested_tool_call, error_message, "Unhandled exception in decorated tool call"
        )


async def execute_tool_calls_and_record_results(message_context: MessageContext, requested_tool_calls: List[ChatCompletionMessageToolCall]):
    """
    Handle tool calls for decorated function tools

    Args:
        message_context: The validated request object
        requested_tool_calls: List of tool calls from GPT
    """
    for requested_tool_call in requested_tool_calls:
        await _execute_single_tool_call(message_context, requested_tool_call)


def build_chat_completions_tool_schemas(tool_names: List[str] = None) -> List[dict]:
    """OpenAI-format tool schemas of the registered ``@llm_tool`` functions.

    ``tool_names``: the tools to include; ``None`` means every registered function.
    """
    function_schemas = FunctionTool.build_tool_schemas(tool_names)
    return [{"type": "function", "function": function_schema} for function_schema in function_schemas]
