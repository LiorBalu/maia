"""
Tenant hooks: give each inbound message the tools of its tenant, then run the tenant's own handler.

Two layers, both optional:

1. ``TenantDocument.tools`` - the tool names chosen on the dashboard (Bot > Tools). Applied to
   every tenant by ``add_tenant_selected_tools``; this is how a business opened from the dashboard
   gets the calendar and sheet tools without any code.
2. ``tenants/<name with '-' → '_'>/handling.py`` exposing ``async def prepare_context_for_<folder>(message_context)``:
   custom code for one business (its own ``function_tools.py`` tools, per-customer logic). The
   folder name is the only registration.
"""

import logging
import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from maia.pipeline.context import ReplyReady, MessageContext
from maia.pipeline.tools.function_decorator import FunctionTool

logger = logging.getLogger(__name__)


def add_tenant_selected_tools(message_context: MessageContext) -> list:
    """Append the tools stored on the tenant document to ``message_context["tool_names"]``.

    Only names registered at startup are bound; a stale name (a tool that was removed from the
    code) is logged and skipped. Returns the names that were added.
    """
    tenant_doc = message_context.get("tenant")
    configured_tool_names = list(getattr(tenant_doc, "tools", None) or [])
    bound_tool_names = []
    for tool_name in configured_tool_names:
        if not FunctionTool.find_tool(tool_name):
            logger.warning("Tool '%s' configured for tenant '%s' is not registered; skipped", tool_name, message_context.get("tenant_name"))
            continue
        if tool_name not in message_context["tool_names"]:
            message_context["tool_names"].append(tool_name)
            bound_tool_names.append(tool_name)
    if bound_tool_names:
        logger.info("Tools from the tenant settings: %s", ", ".join(bound_tool_names))
    return bound_tool_names


def _find_tenant_handler(tenant_name: str) -> Callable[[MessageContext], Awaitable[None]] | None:
    """Import ``tenants/<folder>/handling.py`` and return its ``prepare_context_for_<folder>`` function.

    ``<folder>`` is ``tenant_name`` with ``-`` replaced by ``_``. Returns None when the folder, its
    ``handling.py`` or the function is missing. Nothing is caught here: an error of a broken tenant
    module (ImportError, SyntaxError) reaches the ``except`` of ``_run_tenant_handler``.
    """
    # Convert tenant_name by replacing - with _
    tenant_folder_name = tenant_name.replace("-", "_")
    tenants_folder_path = Path(__file__).parent
    tenant_folder_path = tenants_folder_path / tenant_folder_name

    if not (tenant_folder_path.exists() and tenant_folder_path.is_dir()):
        return None
    # Look for handling.py file
    handling_file_path = tenant_folder_path / "handling.py"
    if not handling_file_path.exists():
        return None
    # Import the module dynamically
    handling_module_name = f"tenants.{tenant_folder_name}.handling"
    handling_module = importlib.import_module(handling_module_name)

    # Look for prepare_context_for_{tenant_folder_name} function
    tenant_handler_name = f"prepare_context_for_{tenant_folder_name}"
    return getattr(handling_module, tenant_handler_name, None)


async def _run_tenant_handler(message_context: MessageContext, tenant_name: str) -> bool:
    """Find and await the tenant's handler; True only when it ran to completion.

    False when the tenant has no handler, or when the lookup, the import or the handler raised: that
    error is logged and swallowed, and the turn goes on with the dashboard tools. A ``ReplyReady``
    raised by the handler propagates unchanged.
    """
    # Dynamic handler discovery - search in the tenants folder
    try:
        tenant_handler = _find_tenant_handler(tenant_name)
        if tenant_handler is not None:
            await tenant_handler(message_context)
            return True
    except ReplyReady:
        raise

    except Exception as error:
        logger.error(f"Error during dynamic handler discovery for '{tenant_name}': {error}")
    return False


async def prepare_context_for_tenant(message_context: MessageContext, tenant_name: str) -> None:
    """
    Bind the tenant's configured tools, then route the request to the tenant's handler (if any).

    Args:
        message_context: The MessageContext of the inbound message
        tenant_name: Tenant identifier for routing

    Returns:
        None: Handlers process the request directly
    """
    add_tenant_selected_tools(message_context)

    if await _run_tenant_handler(message_context, tenant_name):
        return

    # No custom code for this tenant: the dashboard tools (if any) are all it gets.
    if not message_context["tool_names"]:
        logger.info("No tools for tenant '%s': the bot answers from the knowledge base only", tenant_name)
