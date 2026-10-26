"""
Tenant hooks: routes each inbound message to the handler of its tenant.

A tenant ``<name>`` is handled by ``tenants/<name with '-' → '_'>/handling.py`` exposing
``async def handle_<folder>(request)``. The folder name is the only registration; the two
maps below are kept empty on purpose and exist only for an explicit alias that cannot be
derived from a folder name.
"""

import logging
import importlib
from pathlib import Path

from maia.pipeline.context import ReplyReady, MessageContext

logger = logging.getLogger(__name__)

# tuple-of-tenant-names -> handler (franchises / aliases). Empty: no manual aliasing in use.
MULTI_TENANT_HANDLERS: dict = {}

# tenant_name -> handler. Empty: every tenant is resolved by folder-name discovery below.
TENANT_NAME_HANDLERS: dict = {}


async def run_tenant_hooks(request: MessageContext, tenant_name: str) -> None:
    """
    Route the request to the tenant's handler based on tenant_name.

    Args:
        request: The MessageContext of the inbound message
        tenant_name: Tenant identifier for routing

    Returns:
        None: Handlers process the request directly
    """

    # Handle multi-value tenant_name mappings
    for tenant_names, handler in MULTI_TENANT_HANDLERS.items():
        if tenant_name in tenant_names:
            await handler(request)
            return

    # Handle tenant_name based routing
    if tenant_name in TENANT_NAME_HANDLERS:
        await TENANT_NAME_HANDLERS[tenant_name](request)
        return

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

    # Log unhandled cases for debugging
    logger.warning(f"Warning: No handler found for tenant_name='{tenant_name}'")
