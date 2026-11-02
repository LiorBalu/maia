"""The `@gpt_tool` decorator and registry: schema from the type hints, auto-discovery of tool modules, invocation with the injected context."""

import logging
import inspect
import json
import os
import importlib
import glob
from pathlib import Path
import traceback
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union
from functools import wraps
from datetime import datetime, date
from bson import ObjectId
from maia.pipeline.context import ReplyReady

logger = logging.getLogger(__name__)


class FunctionTool:
    """Registry for decorated functions that can be called by GPT"""

    _functions: Dict[str, Any] = {}

    @classmethod
    def register(cls, func):
        """Register a function in the tools registry"""
        cls._functions[func.__name__] = func
        return func

    @classmethod
    def get_function(cls, name: str):
        """Get a registered function by name"""
        return cls._functions.get(name)

    @classmethod
    def get_all_functions(cls):
        """Get all registered functions"""
        return cls._functions

    @classmethod
    def get_tools_schema(cls, function_names: List[str] = None) -> List[Dict]:
        """Convert registered functions to OpenAI tools format

        Args:
            function_names: List of specific function names to get schemas for.
                          If None, returns all registered functions.
        """
        tools = []

        if function_names is None:
            # None means return all functions
            functions_to_process = cls._functions.items()
        elif len(function_names) == 0:
            # Empty list means return no functions
            functions_to_process = []
        else:
            # Filter to only requested functions
            functions_to_process = [(name, func) for name, func in cls._functions.items() if name in function_names]

        for func_name, func in functions_to_process:
            schema = cls._function_to_schema(func)
            tools.append(schema)
        return tools

    @classmethod
    def _function_to_schema(cls, func) -> Dict:
        """Convert a function to OpenAI function schema"""
        # Get function signature and type hints
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        # Build parameters schema
        parameters = {"type": "object", "properties": {}, "required": []}

        for param_name, param in sig.parameters.items():
            # Skip 'request' parameter if it exists
            if param_name == "request":
                continue

            # Skip *args / **kwargs catch-alls — they are not real tool params.
            # Without this, a `**_kwargs` gets emitted as a bogus property and,
            # having no default, lands in `required`, so the model sees a
            # non-existent mandatory argument while the real (defaulted) params
            # look optional.
            if param.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                continue

            param_type = type_hints.get(param_name, str)
            param_schema = cls._type_to_schema(param_type)

            # Add description from docstring if available
            if func.__doc__:
                # Try to extract parameter description from docstring
                param_desc = cls._extract_param_description(func.__doc__, param_name)
                if param_desc:
                    param_schema["description"] = param_desc

            parameters["properties"][param_name] = param_schema

            # Add to required if no default value
            if param.default == inspect.Parameter.empty:
                parameters["required"].append(param_name)

        # Get function description from docstring
        description = func.__doc__.strip().split("\n")[0] if func.__doc__ else f"Call {func.__name__}"

        return {"name": func.__name__, "description": description, "parameters": parameters}

    @classmethod
    def _type_to_schema(cls, param_type) -> Dict:
        """Convert Python type to JSON schema"""
        origin = get_origin(param_type)

        if origin is Union:
            # Handle Optional[Type] which is Union[Type, NoneType]
            args = get_args(param_type)
            if len(args) == 2 and type(None) in args:
                # This is Optional[Type]
                non_none_type = args[0] if args[1] is type(None) else args[1]
                return cls._type_to_schema(non_none_type)
            else:
                # Multiple union types, default to string
                return {"type": "string"}

        elif origin is list or param_type is list:
            if origin is list:
                args = get_args(param_type)
                if args:
                    item_schema = cls._type_to_schema(args[0])
                    return {"type": "array", "items": item_schema}
            return {"type": "array", "items": {"type": "string"}}

        elif origin is dict or param_type is dict:
            return {"type": "object"}

        elif param_type is str:
            return {"type": "string"}
        elif param_type is int:
            return {"type": "integer"}
        elif param_type is float:
            return {"type": "number"}
        elif param_type is bool:
            return {"type": "boolean"}
        else:
            # Default to string for unknown types
            return {"type": "string"}

    @classmethod
    def _extract_param_description(cls, docstring: str, param_name: str) -> str:
        """Extract parameter description from docstring"""
        lines = docstring.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith(f"{param_name}:"):
                return line.split(":", 1)[1].strip()
        return ""

    @classmethod
    def discover_tools(cls):
        """Discover and import all tools from tenants/*/function_tools.py and maia/pipeline/tools/**/*.py files

        This should be called once at application startup.
        """
        try:
            # Get the project root directory
            current_file = Path(__file__)
            project_root = current_file.parent.parent.parent.parent  # repository root

            # Patterns to match tool files
            patterns = [
                str(project_root / "tenants" / "*" / "function_tools.py"),
                str(project_root / "maia" / "pipeline" / "tools" / "**" / "*.py"),
            ]

            tools_files = []
            for pattern in patterns:
                tools_files.extend(glob.glob(pattern, recursive=True))

            # Filter out __init__.py and function_decorator.py itself
            tools_files = [
                f for f in tools_files if not f.endswith("__init__.py") and not f.endswith("function_decorator.py")
            ]

            logger.info(f"Auto-discovering tools: Found {len(tools_files)} tool files")

            for tools_file in tools_files:
                try:
                    # Convert path to module name
                    relative_path = Path(tools_file).relative_to(project_root)
                    module_name = str(relative_path.with_suffix("")).replace(os.sep, ".")

                    # Import the module
                    importlib.import_module(module_name)
                    logger.info(f"Imported {module_name}")

                except Exception as e:
                    logger.error(f"Failed to import {tools_file}: {e}")
                    continue

            logger.info(f"Auto-discovery complete. Total registered functions: {len(cls._functions)}")

        except Exception as e:
            logger.error(f"Error during auto-discovery: {e}")


def gpt_tool(func):
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

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # If the function is async, await it
        """Await the tool whether it is async or sync."""
        if inspect.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)

    # Register the original function (not the wrapper) for schema generation
    FunctionTool.register(func)

    return wrapper


# Public function for easy import in main.py
def discover_all_tools():
    """
    Discover and register all decorated tools from tenants/*/tools.py files.

    This should be called once at application startup.
    """
    FunctionTool.discover_tools()


def json_serializer(obj):
    """
    Custom JSON serializer for objects not serializable by default json code

    Handles:
    - MongoDB ObjectId -> string
    - datetime/date -> ISO format string
    """
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


async def call_function_tool(function_name: str, arguments: Dict, request=None) -> str:
    """
    Call a registered function tool with the given arguments

    Args:
        function_name: Name of the function to call
        arguments: Dictionary of arguments to pass to the function
        request: Optional request object to pass to functions that need it

    Returns:
        String result from the function call
    """
    func = FunctionTool.get_function(function_name)
    if not func:
        return f"Function {function_name} not found"

    try:
        # Get function signature
        sig = inspect.signature(func)

        # The model's arguments are passed as they arrive (JSON types: str, int, float, bool, list, dict)
        converted_arguments = dict(arguments)

        # Check if function expects 'request' parameter
        if "request" in sig.parameters and request is not None:
            converted_arguments["request"] = request

        # Call the function
        if inspect.iscoroutinefunction(func):
            result = await func(**converted_arguments)
        else:
            result = func(**converted_arguments)

        # Convert result to string if it's not already
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False, default=json_serializer)
        else:
            return str(result)

    except ReplyReady as se:
        raise se

    except Exception as e:
        return f"Error calling {function_name}: {str(e)} {traceback.format_exc()}"
