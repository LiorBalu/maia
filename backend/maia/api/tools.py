"""The tools a business can give its bot: the `@llm_tool` functions registered at startup.

The dashboard lists them as checkboxes and saves the chosen names in ``TenantDocument.tools``
(``PUT /management/v1/tenants/{tenant}``); ``tenants/hooks.py`` binds them on every turn.
"""

from typing import Any

from fastapi import APIRouter, Depends

from maia.models.users import User
from maia.api.security import authenticate_current_user
from maia.pipeline.tools.function_decorator import FunctionTool

tools_router = APIRouter(prefix="/tools", tags=["Tools"])


def _extract_short_description(tool_function) -> str:
    """First non-empty line of the tool's docstring (the full docstring is the LLM's description)."""
    for docstring_line in (tool_function.__doc__ or "").splitlines():
        if docstring_line.strip():
            return docstring_line.strip()
    return ""


def build_registered_tools_catalog() -> list[dict[str, Any]]:
    """Every registered tool: ``name``, ``module`` (file it lives in, e.g. ``google_calendar``) and ``description``."""
    return [
        {
            "name": tool_name,
            "module": tool_function.__module__.rsplit(".", 1)[-1],
            "description": _extract_short_description(tool_function),
        }
        for tool_name, tool_function in FunctionTool.registered_tools_by_name().items()
    ]


@tools_router.get("", summary="Tools a business can give its bot")
async def list_registered_tools(current_user: User = Depends(authenticate_current_user)):
    """The registered tools, in registration order (calendar tools first, then the sheet lookup, then tenant tools)."""
    return build_registered_tools_catalog()
