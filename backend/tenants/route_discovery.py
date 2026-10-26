"""Auto-discovery of `tenants/<folder>/routes.py` routers, mounted under `/custom-api/<folder-with-dashes>`."""

import logging
import glob
import importlib
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI

logger = logging.getLogger(__name__)


def discover_tenant_routes(app: FastAPI, prefix: str = "/custom-api"):
    """
    Auto-discover and register all tenant routes from tenants/*/routes.py files.
    Each file is imported and any APIRouter instance found is included in the app.
    If the router doesn't already have a prefix, one is generated from the folder name.

    Should be called once at application startup.
    """
    project_root = Path(__file__).parent.parent  # repository root
    tenants_dir = project_root / "tenants"

    pattern = str(tenants_dir / "*" / "routes.py")
    route_files = glob.glob(pattern)

    logger.info(f"Auto-discovering tenant routes: Found {len(route_files)} route files")

    registered_count = 0
    for route_file in sorted(route_files):
        try:
            relative_path = Path(route_file).relative_to(project_root)
            module_name = str(relative_path.with_suffix("")).replace(os.sep, ".")
            folder_name = Path(route_file).parent.name

            module = importlib.import_module(module_name)

            # Find all APIRouter instances in the module
            routers_found = []
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, APIRouter):
                    routers_found.append((attr_name, attr))

            for attr_name, router in routers_found:
                # Build the route prefix: /custom-api/{folder_name}
                # If the router already has its own prefix, it will be appended
                router_prefix = f"/{folder_name.replace('_', '-')}"
                tag = folder_name.replace("_", " ").title()

                app.include_router(
                    router,
                    prefix=f"{prefix}{router_prefix}",
                    tags=[tag],
                )
                logger.info(f"  Registered router '{attr_name}' from {module_name} at {prefix}{router_prefix}")
                registered_count += 1

        except Exception as e:
            logger.error(f"  Failed to import {route_file}: {e}")
            continue

    logger.info(f"Route auto-discovery complete. Registered {registered_count} routers.")
