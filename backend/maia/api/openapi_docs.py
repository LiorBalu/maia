"""OpenAPI schema and the Swagger UI page behind Basic Auth; the tenant enum is filled from the DB."""

import copy

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from maia.api.security import authenticate_docs_user

router = APIRouter()


def _restrict_tenant_name_parameter(parameter_definition: dict, known_tenant_names: list) -> None:
    """Set known_tenant_names as the enum of one tenant_name path parameter and append a note to its description."""
    parameter_definition.setdefault("schema", {})["enum"] = known_tenant_names
    parameter_definition["description"] = (
        (parameter_definition.get("description") or "") + "  \n**Valid values (live from DB):** see enum below."
    ).strip()


def _attach_tenant_enum_to_tenant_name_parameters(openapi_schema: dict, known_tenant_names: list) -> dict:
    """Inject known_tenant_names as an enum into every tenant_name path parameter."""
    if not known_tenant_names:
        return openapi_schema
    for path_item_definition in openapi_schema.get("paths", {}).values():
        for operation_definition in path_item_definition.values():
            if not isinstance(operation_definition, dict):
                continue
            for parameter_definition in operation_definition.get("parameters", []):
                if parameter_definition.get("name") == "tenant_name" and parameter_definition.get("in") == "path":
                    _restrict_tenant_name_parameter(parameter_definition, known_tenant_names)
    return openapi_schema


def _load_known_tenant_names() -> list:
    """Sorted names of every tenant in Mongo; [] when the import or the query fails."""
    try:
        from maia.models.tenants import TenantDocument

        return sorted(tenant_doc.name for tenant_doc in TenantDocument.objects().only("name") if tenant_doc.name)
    except Exception:
        return []


@router.get("/openapi.json", include_in_schema=False)
async def render_openapi_schema_for_staff(http_request: Request, current_user=Depends(authenticate_docs_user)):
    """Full OpenAPI schema for staff users with all tenant names in the enum."""
    if not getattr(current_user, "is_staff", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff access required.")

    openapi_schema = copy.deepcopy(http_request.app.openapi())

    return JSONResponse(_attach_tenant_enum_to_tenant_name_parameters(openapi_schema, _load_known_tenant_names()))


# ---------------------------------------------------------------------------
# Swagger UI
# ---------------------------------------------------------------------------


@router.get("/docs", include_in_schema=False)
async def render_api_docs_page(current_user=Depends(authenticate_docs_user)):
    """Swagger UI page, behind Basic Auth."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Maia API docs")
