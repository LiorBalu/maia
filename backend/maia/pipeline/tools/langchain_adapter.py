"""Wraps registered `@llm_tool` functions as LangChain tools so the RAG chain can call them and the calls get saved."""

import inspect
import json
import uuid
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.tools import StructuredTool
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

from maia.pipeline.tools.function_decorator import FunctionTool


def _find_registered_tool_functions(tool_names: Optional[List[str]]) -> List[Tuple[str, Callable]]:
    """(name, function) of each requested tool that is registered, in the requested order; [] when no name is given."""
    registered_tool_functions = []

    if tool_names:
        registered_tool_functions = [
            (tool_name, FunctionTool.find_tool(tool_name))
            for tool_name in tool_names
            if FunctionTool.find_tool(tool_name)
        ]

    return registered_tool_functions


def build_langchain_tools(
    tool_names: Optional[List[str]] = None, message_context: Optional[Dict[str, Any]] = None
) -> List[StructuredTool]:
    """Wrap the requested ``@llm_tool`` functions as LangChain StructuredTools (decorated tools only)."""
    structured_tools = []

    for tool_name, tool_function in _find_registered_tool_functions(tool_names):
        tool_coroutine = _wrap_tool_for_langchain(tool_function, message_context)
        structured_tool = StructuredTool.from_function(
            coroutine=tool_coroutine, name=tool_name, description=tool_function.__doc__ or f"Tool: {tool_name}"
        )
        structured_tools.append(structured_tool)

    return structured_tools


def _build_signature_without_request(tool_function: Callable) -> inspect.Signature:
    """The tool's signature without the injected ``request`` parameter (what LangChain builds the JSON schema from)."""
    tool_signature = inspect.signature(tool_function)

    # Get all parameters except 'request'
    exposed_parameters = [
        parameter for parameter_name, parameter in tool_signature.parameters.items() if parameter_name != "request"
    ]

    return inspect.Signature(parameters=exposed_parameters)


def _build_and_record_tool_call(
    message_context: Dict[str, Any], tool_name: str, tool_arguments: Dict[str, Any]
) -> ChatCompletionMessageToolCall:
    """Build the tool call (a fresh uuid as its id), save it as the role=tool_calls message of the conversation and
    return it: its id keys the result in ``message_context["tool_results"]``.
    """
    # Generate a unique tool_call_id for this tool call
    tool_call_id = str(uuid.uuid4())

    # Create a ChatCompletionMessageToolCall object
    openai_tool_call = ChatCompletionMessageToolCall(
        id=tool_call_id, type="function", function=Function(name=tool_name, arguments=json.dumps(tool_arguments))
    )

    # Save tool call payload to database
    from maia.pipeline.tool_messages import build_assistant_tool_call_message, record_tool_call_in_conversation

    tool_call_message_payload = build_assistant_tool_call_message(openai_tool_call)
    record_tool_call_in_conversation(message_context, tool_call_message_payload)

    return openai_tool_call


def _record_tool_result(
    message_context: Dict[str, Any],
    openai_tool_call: ChatCompletionMessageToolCall,
    tool_name: str,
    tool_arguments: Dict[str, Any],
    tool_result: Any,
) -> None:
    """Store the tool's result under the call's id in ``message_context["tool_results"]``, save it as the role=tool
    message of the conversation and append the call to ``message_context["tool_calls_log"]``.
    """
    from maia.pipeline.tool_messages import build_tool_response_message, record_tool_response_in_conversation

    # Store the result in tool_results for saving
    if not message_context.get("tool_results"):
        message_context["tool_results"] = {}
    message_context["tool_results"][openai_tool_call.id] = str(tool_result)

    # Save tool response to database
    tool_response_message = build_tool_response_message(message_context, openai_tool_call)
    record_tool_response_in_conversation(message_context, tool_response_message)

    message_context.setdefault("tool_calls_log", []).append(
        {tool_name: {"params": tool_arguments, "response": str(tool_result)}}
    )


def _wrap_tool_for_langchain(tool_function, message_context):
    """
    Create a wrapper function that excludes the 'request' parameter from the signature.
    This allows LangChain to generate a valid JSON schema without MongoDB models.
    """
    signature_without_request = _build_signature_without_request(tool_function)

    # Create wrapper function with modified signature
    @wraps(tool_function)
    async def langchain_tool_coroutine(**tool_arguments):
        """Run the tool for LangChain: save the call as a tool_calls message, call the original function with the
        injected request, store the result in message_context[tool_results], save the tool message and log the call.
        """
        # Get the function name
        tool_name = tool_function.__name__

        openai_tool_call = _build_and_record_tool_call(message_context, tool_name, tool_arguments)

        # Inject the request parameter when calling the original function
        tool_result = await tool_function(request=message_context, **tool_arguments)

        _record_tool_result(message_context, openai_tool_call, tool_name, tool_arguments, tool_result)

        return tool_result

    # Update the wrapper's signature to exclude 'request'
    langchain_tool_coroutine.__signature__ = signature_without_request

    return langchain_tool_coroutine
