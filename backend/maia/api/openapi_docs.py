"""OpenAPI schema and Swagger / RapiDoc / Scalar pages behind Basic Auth; the tenant enum is filled from the DB."""

import copy

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from maia.api.security import verify_basic_auth

router = APIRouter()


def _inject_tenant_enum(schema: dict, tenant_names: list) -> dict:
    """Inject tenant_names as an enum into every tenant_name path parameter."""
    if not tenant_names:
        return schema
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for param in operation.get("parameters", []):
                if param.get("name") == "tenant_name" and param.get("in") == "path":
                    param.setdefault("schema", {})["enum"] = tenant_names
                    param["description"] = (
                        (param.get("description") or "") + "  \n**Valid values (live from DB):** see enum below."
                    ).strip()
    return schema


@router.get("/openapi.json", include_in_schema=False)
async def get_staff_openapi(request: Request, user=Depends(verify_basic_auth)):
    """Full OpenAPI schema for staff users with all tenant names in the enum."""
    if not getattr(user, "is_staff", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required.")

    schema = copy.deepcopy(request.app.openapi())

    try:
        from maia.models.tenants import TenantDocument

        all_tenants = sorted(s.name for s in TenantDocument.objects().only("name") if s.name)
    except Exception:
        all_tenants = []

    return JSONResponse(_inject_tenant_enum(schema, all_tenants))


# ---------------------------------------------------------------------------
# Swagger UI
# ---------------------------------------------------------------------------


@router.get("/docs", include_in_schema=False)
async def get_docs(user=Depends(verify_basic_auth)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Maia API docs")


# ---------------------------------------------------------------------------
# RapiDoc — standard (sidebar layout, persistent auth)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RapiDoc — focused render style (modern single-endpoint view)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scalar — modern design, native OAuth2 password flow, built-in request history
# ---------------------------------------------------------------------------
