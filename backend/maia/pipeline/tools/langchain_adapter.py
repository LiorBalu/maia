"""Wraps registered `@gpt_tool` functions as LangChain tools so the RAG chain can call them and the calls get saved."""

import inspect
import json
from functools import wraps
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

from maia.pipeline.tools.function_decorator import FunctionTool


def convert_to_langchain_tools(
    function_names: Optional[List[str]] = None, request: Optional[Dict[str, Any]] = None
) -> List[StructuredTool]:
    """Wrap the requested ``@gpt_tool`` functions as LangChain StructuredTools (decorated tools only)."""
    langchain_tools = []
    functions_to_process = []

    if function_names:
        functions_to_process = [
            (name, FunctionTool.get_function(name)) for name in function_names if FunctionTool.get_function(name)
        ]

    for func_name, func in functions_to_process:
        wrapped_func = _create_wrapped_function(func, request)
        tool = StructuredTool.from_function(
            coroutine=wrapped_func, name=func_name, description=func.__doc__ or f"Tool: {func_name}"
        )
        langchain_tools.append(tool)

    return langchain_tools


def _create_wrapped_function(original_func, request):
    """
    Create a wrapper function that excludes the 'request' parameter from the signature.
    This allows LangChain to generate a valid JSON schema without MongoDB models.
    """
    sig = inspect.signature(original_func)

    # Get all parameters except 'request'
    new_params = [param for name, param in sig.parameters.items() if name != "request"]

    # Create wrapper function with modified signature
    @wraps(original_func)
    async def wrapper(**kwargs):
        # Generate a unique call_id for this tool call
        """Run the tool for LangChain: save the call as a tool_calls message, call the original function with the
        injected request, store the result in request[tool_results], save the tool message and log the call.
        """
        import uuid

        call_id = str(uuid.uuid4())

        # Get the function name
        func_name = original_func.__name__

        # Create a ChatCompletionMessageToolCall object
        tool_call = ChatCompletionMessageToolCall(
            id=call_id, type="function", function=Function(name=func_name, arguments=json.dumps(kwargs))
        )

        # Save tool call payload to database
        from maia.pipeline.tool_messages import (
            generate_tool_call_payload,
            generate_tool_response,
            save_tool_call_payload,
            save_tool_response,
        )

        tool_call_payload = generate_tool_call_payload(tool_call)
        save_tool_call_payload(request, tool_call_payload)

        # Inject the request parameter when calling the original function
        result = await original_func(request=request, **kwargs)

        # Store the result in tools_response for saving
        if not request.get("tool_results"):
            request["tool_results"] = {}
        request["tool_results"][call_id] = str(result)

        # Save tool response to database
        tool_response = generate_tool_response(request, tool_call)
        save_tool_response(request, tool_response)

        request.setdefault("tool_calls_log", []).append({func_name: {"params": kwargs, "response": str(result)}})

        return result

    # Update the wrapper's signature to exclude 'request'
    wrapper.__signature__ = inspect.Signature(parameters=new_params)

    return wrapper
