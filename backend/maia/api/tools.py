"""The tools a business can give its bot: the `@gpt_tool` functions registered at startup.

The dashboard lists them as checkboxes and saves the chosen names in ``TenantDocument.tools``
(``PUT /management/v1/tenants/{tenant}``); ``tenants/hooks.py`` binds them on every turn.
"""

from typing import Any

from fastapi import APIRouter, Depends

from maia.models.users import User
from maia.api.security import authenticate_user_token
from maia.pipeline.tools.function_decorator import FunctionTool

tools_router = APIRouter(prefix="/tools", tags=["Tools"])


def _description(func) -> str:
    """First non-empty line of the tool's docstring (the full docstring is the LLM's description)."""
    for line in (func.__doc__ or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def available_tools() -> list[dict[str, Any]]:
    """Every registered tool: ``name``, ``module`` (file it lives in, e.g. ``google_calendar``) and ``description``."""
    return [
        {"name": name, "module": func.__module__.rsplit(".", 1)[-1], "description": _description(func)}
        for name, func in FunctionTool.get_all_functions().items()
    ]


@tools_router.get("", summary="Tools a business can give its bot")
async def list_tools(user: User = Depends(authenticate_user_token)):
    """The registered tools, in registration order (calendar tools first, then the sheet lookup, then tenant tools)."""
    return available_tools()
