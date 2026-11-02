"""
Tenant hooks: give each inbound message the tools of its tenant, then run the tenant's own handler.

Two layers, both optional:

1. ``TenantDocument.tools`` - the tool names chosen on the dashboard (Bot > Tools). Applied to
   every tenant by ``apply_tenant_tools``; this is how a business opened from the dashboard
   gets the calendar and sheet tools without any code.
2. ``tenants/<name with '-' → '_'>/handling.py`` exposing ``async def handle_<folder>(request)``:
   custom code for one business (its own ``function_tools.py`` tools, per-customer logic). The
   folder name is the only registration.
"""

import logging
import importlib
from pathlib import Path

from maia.pipeline.context import ReplyReady, MessageContext
from maia.pipeline.tools.function_decorator import FunctionTool

logger = logging.getLogger(__name__)


def apply_tenant_tools(request: MessageContext) -> list:
    """Append the tools stored on the tenant document to ``request["tool_names"]``.

    Only names registered at startup are bound; a stale name (a tool that was removed from the
    code) is logged and skipped. Returns the names that were added.
    """
    tenant = request.get("tenant")
    configured = list(getattr(tenant, "tools", None) or [])
    added = []
    for name in configured:
        if not FunctionTool.get_function(name):
            logger.warning("Tool '%s' configured for tenant '%s' is not registered; skipped", name, request.get("tenant_name"))
            continue
        if name not in request["tool_names"]:
            request["tool_names"].append(name)
            added.append(name)
    if added:
        logger.info("Tools from the tenant settings: %s", ", ".join(added))
    return added


async def run_tenant_hooks(request: MessageContext, tenant_name: str) -> None:
    """
    Bind the tenant's configured tools, then route the request to the tenant's handler (if any).

    Args:
        request: The MessageContext of the inbound message
        tenant_name: Tenant identifier for routing

    Returns:
        None: Handlers process the request directly
    """
    apply_tenant_tools(request)

    # Dynamic handler discovery - search in the tenants folder
    try:
        # Convert tenant_name by replacing - with _
        folder_name = tenant_name.replace("-", "_")
        tenants_dir = Path(__file__).parent
        handler_dir = tenants_dir / folder_name

        if handler_dir.exists() and handler_dir.is_dir():
            # Look for handling.py file
            handling_file = handler_dir / "handling.py"
            if handling_file.exists():
                # Import the module dynamically
                module_path = f"tenants.{folder_name}.handling"
                module = importlib.import_module(module_path)

                # Look for handle_{tenant_name} function
                handler_function_name = f"handle_{folder_name}"
                if hasattr(module, handler_function_name):
                    handler = getattr(module, handler_function_name)
                    await handler(request)
                    return
    except ReplyReady as se:
        raise se

    except Exception as e:
        logger.error(f"Error during dynamic handler discovery for '{tenant_name}': {e}")

    # No custom code for this tenant: the dashboard tools (if any) are all it gets.
    if not request["tool_names"]:
        logger.info("No tools for tenant '%s': the bot answers from the knowledge base only", tenant_name)
