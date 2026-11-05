"""The `@llm_tool` decorator and registry: schema from the type hints, auto-discovery of tool modules, invocation with the injected context."""

import logging
import inspect
import json
import os
import importlib
import glob
from pathlib import Path
import traceback
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Optional, Union
from functools import wraps
from datetime import datetime, date
from bson import ObjectId
from maia.pipeline.context import ReplyReady

logger = logging.getLogger(__name__)


class FunctionTool:
    """Registry for decorated functions that can be called by GPT"""

    _tools_by_name: Dict[str, Any] = {}

    @classmethod
    def register_tool(cls, tool_function):
        """Register a function in the tools registry"""
        cls._tools_by_name[tool_function.__name__] = tool_function
        return tool_function

    @classmethod
    def find_tool(cls, tool_name: str):
        """Get a registered function by name"""
        return cls._tools_by_name.get(tool_name)

    @classmethod
    def registered_tools_by_name(cls):
        """Get all registered functions"""
        return cls._tools_by_name

    @classmethod
    def _select_registered_tools(cls, tool_names: Optional[List[str]]):
        """The registered (name, function) pairs to build schemas for: all of them for ``None``, none for an empty
        list, otherwise those whose name is in ``tool_names`` (in registration order).
        """
        if tool_names is None:
            # None means return all functions
            return cls._tools_by_name.items()
        elif len(tool_names) == 0:
            # Empty list means return no functions
            return []
        else:
            # Filter to only requested functions
            return [
                (tool_name, tool_function)
                for tool_name, tool_function in cls._tools_by_name.items()
                if tool_name in tool_names
            ]

    @classmethod
    def build_tool_schemas(cls, tool_names: List[str] = None) -> List[Dict]:
        """Convert registered functions to OpenAI tools format

        Args:
            tool_names: List of specific function names to get schemas for.
                          If None, returns all registered functions.
        """
        tool_schemas = []

        selected_tools = cls._select_registered_tools(tool_names)

        for _tool_name, tool_function in selected_tools:
            tool_schema = cls._build_schema_from_signature(tool_function)
            tool_schemas.append(tool_schema)
        return tool_schemas

    @staticmethod
    def _is_tool_parameter(parameter_name: str, parameter: inspect.Parameter) -> bool:
        """True when the parameter belongs in the tool schema: not the injected ``request``, not a catch-all."""
        # Skip 'request' parameter if it exists
        if parameter_name == "request":
            return False

        # Skip *args / **kwargs catch-alls — they are not real tool params.
        # Without this, a `**_kwargs` gets emitted as a bogus property and,
        # having no default, lands in `required`, so the model sees a
        # non-existent mandatory argument while the real (defaulted) params
        # look optional.
        if parameter.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            return False

        return True

    @classmethod
    def _build_parameter_schema(cls, parameter_name: str, parameter_type: Any, tool_docstring: Optional[str]) -> Dict:
        """JSON schema of one tool parameter: its type, plus its ``<name>:`` docstring line as the description."""
        parameter_schema = cls._map_type_to_json_schema(parameter_type)

        # Add description from docstring if available
        if tool_docstring:
            # Try to extract parameter description from docstring
            parameter_description = cls._find_parameter_description(tool_docstring, parameter_name)
            if parameter_description:
                parameter_schema["description"] = parameter_description

        return parameter_schema

    @classmethod
    def _build_schema_from_signature(cls, tool_function) -> Dict:
        """Convert a function to OpenAI function schema"""
        # Get function signature and type hints
        signature = inspect.signature(tool_function)
        type_hints = get_type_hints(tool_function)

        # Build parameters schema
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        for parameter_name, parameter in signature.parameters.items():
            if not cls._is_tool_parameter(parameter_name, parameter):
                continue

            parameter_type = type_hints.get(parameter_name, str)
            parameters_schema["properties"][parameter_name] = cls._build_parameter_schema(
                parameter_name, parameter_type, tool_function.__doc__
            )

            # Add to required if no default value
            if parameter.default == inspect.Parameter.empty:
                parameters_schema["required"].append(parameter_name)

        # Get function description from docstring
        tool_description = (
            tool_function.__doc__.strip().split("\n")[0] if tool_function.__doc__ else f"Call {tool_function.__name__}"
        )

        return {"name": tool_function.__name__, "description": tool_description, "parameters": parameters_schema}

    @classmethod
    def _map_union_to_json_schema(cls, python_type) -> Dict:
        """JSON schema of a ``Union``: ``Optional[T]`` maps like ``T``, any other union to a string."""
        # Handle Optional[Type] which is Union[Type, NoneType]
        member_types = get_args(python_type)
        if len(member_types) == 2 and type(None) in member_types:
            # This is Optional[Type]
            inner_type = member_types[0] if member_types[1] is type(None) else member_types[1]
            return cls._map_type_to_json_schema(inner_type)
        else:
            # Multiple union types, default to string
            return {"type": "string"}

    @classmethod
    def _map_list_to_json_schema(cls, python_type) -> Dict:
        """JSON schema of a list: an array of the first type argument's schema, of strings for a bare ``list``."""
        if get_origin(python_type) is list:
            item_types = get_args(python_type)
            if item_types:
                item_schema = cls._map_type_to_json_schema(item_types[0])
                return {"type": "array", "items": item_schema}
        return {"type": "array", "items": {"type": "string"}}

    @classmethod
    def _map_type_to_json_schema(cls, python_type) -> Dict:
        """Convert Python type to JSON schema"""
        type_origin = get_origin(python_type)

        if type_origin is Union:
            return cls._map_union_to_json_schema(python_type)

        elif type_origin is list or python_type is list:
            return cls._map_list_to_json_schema(python_type)

        elif type_origin is dict or python_type is dict:
            return {"type": "object"}

        elif python_type is str:
            return {"type": "string"}
        elif python_type is int:
            return {"type": "integer"}
        elif python_type is float:
            return {"type": "number"}
        elif python_type is bool:
            return {"type": "boolean"}
        else:
            # Default to string for unknown types
            return {"type": "string"}

    @classmethod
    def _find_parameter_description(cls, tool_docstring: str, parameter_name: str) -> str:
        """Extract parameter description from docstring"""
        docstring_lines = tool_docstring.split("\n")
        for docstring_line in docstring_lines:
            stripped_line = docstring_line.strip()
            if stripped_line.startswith(f"{parameter_name}:"):
                return stripped_line.split(":", 1)[1].strip()
        return ""

    @staticmethod
    def _find_tool_files(backend_root: Path) -> List[str]:
        """Paths of the tool modules (tenants/*/function_tools.py, maia/pipeline/tools/**/*.py), without the package
        ``__init__.py`` files and this registry module.
        """
        # Patterns to match tool files
        tool_file_patterns = [
            str(backend_root / "tenants" / "*" / "function_tools.py"),
            str(backend_root / "maia" / "pipeline" / "tools" / "**" / "*.py"),
        ]

        tool_file_paths = []
        for tool_file_pattern in tool_file_patterns:
            tool_file_paths.extend(glob.glob(tool_file_pattern, recursive=True))

        # Filter out __init__.py and function_decorator.py itself
        return [
            tool_file_path
            for tool_file_path in tool_file_paths
            if not tool_file_path.endswith("__init__.py") and not tool_file_path.endswith("function_decorator.py")
        ]

    @staticmethod
    def _import_tool_module(backend_root: Path, tool_file_path: str) -> None:
        """Import one tool module by its dotted name (which registers its decorated tools); a failure is only logged."""
        try:
            # Convert path to module name
            relative_module_path = Path(tool_file_path).relative_to(backend_root)
            dotted_module_name = str(relative_module_path.with_suffix("")).replace(os.sep, ".")

            # Import the module
            importlib.import_module(dotted_module_name)
            logger.info(f"Imported {dotted_module_name}")

        except Exception as import_error:
            logger.error(f"Failed to import {tool_file_path}: {import_error}")

    @classmethod
    def import_tool_modules(cls):
        """Discover and import all tools from tenants/*/function_tools.py and maia/pipeline/tools/**/*.py files

        This should be called once at application startup.
        """
        try:
            # Get the project root directory
            this_module_path = Path(__file__)
            backend_root = this_module_path.parent.parent.parent.parent  # repository root

            tool_file_paths = cls._find_tool_files(backend_root)

            logger.info(f"Auto-discovering tools: Found {len(tool_file_paths)} tool files")

            for tool_file_path in tool_file_paths:
                cls._import_tool_module(backend_root, tool_file_path)

            logger.info(f"Auto-discovery complete. Total registered functions: {len(cls._tools_by_name)}")

        except Exception as discovery_error:
            logger.error(f"Error during auto-discovery: {discovery_error}")


def llm_tool(tool_function):
    """
    Decorator to register a function as a GPT tool

    The function should have type hints and a docstring.
    The docstring format should be:

    '''
    Brief description of what the function does

    param_name: Description of parameter
    other_param: Description of other parameter
    '''
    """

    @wraps(tool_function)
    async def async_tool_wrapper(*args, **kwargs):
        """Await the tool whether it is async or sync."""
        return await _run_tool(tool_function, *args, **kwargs)

    # Register the original function (not the wrapper) for schema generation
    FunctionTool.register_tool(tool_function)

    return async_tool_wrapper


# Public function for easy import in main.py
def register_all_tools():
    """
    Discover and register all decorated tools from tenants/*/tools.py files.

    This should be called once at application startup.
    """
    FunctionTool.import_tool_modules()


def serialize_object_id_or_datetime(value):
    """
    Custom JSON serializer for objects not serializable by default json code

    Handles:
    - MongoDB ObjectId -> string
    - datetime/date -> ISO format string
    """
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Type {type(value)} not serializable")


def _bind_tool_arguments(tool_function, tool_arguments: Dict, message_context) -> Dict:
    """Keyword arguments of the call: the model's arguments, plus the message context as ``request`` when the tool
    declares that parameter and a context was given.
    """
    # Get function signature
    tool_signature = inspect.signature(tool_function)

    # The model's arguments are passed as they arrive (JSON types: str, int, float, bool, list, dict)
    call_kwargs = dict(tool_arguments)

    # Check if function expects 'request' parameter
    if "request" in tool_signature.parameters and message_context is not None:
        call_kwargs["request"] = message_context

    return call_kwargs


async def _run_tool(tool_function, /, *args, **call_kwargs):
    """Call the tool, awaiting it when it is a coroutine function, and return its raw result."""
    # If the function is async, await it
    if inspect.iscoroutinefunction(tool_function):
        return await tool_function(*args, **call_kwargs)
    else:
        return tool_function(*args, **call_kwargs)


def _render_tool_result(tool_result) -> str:
    """The tool's result as the text handed back to the model: JSON for a dict or a list, ``str()`` otherwise."""
    # Convert result to string if it's not already
    if isinstance(tool_result, (dict, list)):
        return json.dumps(tool_result, ensure_ascii=False, default=serialize_object_id_or_datetime)
    else:
        return str(tool_result)


async def execute_tool_by_name(tool_name: str, tool_arguments: Dict, message_context=None) -> str:
    """
    Call a registered function tool with the given arguments

    Args:
        tool_name: Name of the function to call
        tool_arguments: Dictionary of arguments to pass to the function
        message_context: Optional request object to pass to functions that need it

    Returns:
        String result from the function call
    """
    tool_function = FunctionTool.find_tool(tool_name)
    if not tool_function:
        return f"Function {tool_name} not found"

    try:
        call_kwargs = _bind_tool_arguments(tool_function, tool_arguments, message_context)

        # Call the function
        tool_result = await _run_tool(tool_function, **call_kwargs)

        return _render_tool_result(tool_result)

    except ReplyReady as reply_ready:
        raise reply_ready

    except Exception as tool_error:
        return f"Error calling {tool_name}: {str(tool_error)} {traceback.format_exc()}"
