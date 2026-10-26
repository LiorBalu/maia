"""Runs the tool calls the model requested (Realtime phone sessions) and builds the tool schemas from the registry."""

import json
import logging
import traceback
from typing import List

from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall

from maia.pipeline.tools.function_decorator import FunctionTool, call_function_tool
from maia.pipeline.context import MessageContext

logger = logging.getLogger(__name__)


async def run_tool_calls(request: MessageContext, tool_calls: List[ChatCompletionMessageToolCall]):
    """
    Handle tool calls for decorated function tools

    Args:
        request: The validated request object
        tool_calls: List of tool calls from GPT
    """
    for tool_call in tool_calls:
        function_name = tool_call.function.name

        # Check if this is a decorated function tool
        if FunctionTool.get_function(function_name):
            try:
                # Parse arguments
                arguments = json.loads(tool_call.function.arguments)

                # Call the function
                result = await call_function_tool(function_name, arguments, request)

                # Store the result in tools_response
                request["tool_results"][tool_call.id] = result

                logger.info(f"Decorated tool {function_name} called successfully with result: {result[:100]}...")

            except json.JSONDecodeError as e:
                error_msg = f"Error parsing arguments for {function_name}: {str(e)}"
                request["tool_results"][tool_call.id] = error_msg
                logger.error(
                    "JSON decode error in decorated tool call\n"
                    "Function   : %s\n"
                    "Tenant      : %s\n"
                    "From       : %s\n"
                    "Traceback  :\n%s",
                    function_name,
                    request.get("tenant_name"),
                    request.get("sender_id"),
                    traceback.format_exc(),
                )

            except Exception as e:
                error_msg = f"Error executing {function_name}: {str(e)}"
                request["tool_results"][tool_call.id] = error_msg
                logger.error(
                    "Unhandled exception in decorated tool call\n"
                    "Function   : %s\n"
                    "Tenant      : %s\n"
                    "From       : %s\n"
                    "Traceback  :\n%s",
                    function_name,
                    request.get("tenant_name"),
                    request.get("sender_id"),
                    traceback.format_exc(),
                )


def tool_schemas_for(function_names: List[str] = None) -> List[dict]:
    """OpenAI-format tool schemas of the registered ``@gpt_tool`` functions.

    ``function_names``: the tools to include; ``None`` means every registered function.
    """
    tools = FunctionTool.get_tools_schema(function_names)
    return [{"type": "function", "function": tool} for tool in tools]
