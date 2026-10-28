"""Google Sheets connection of a tenant: which spreadsheet the bot may read, with a live preview.

The dashboard (Settings > Google Sheets) saves the link, shows the service-account address the
sheet has to be shared with, and previews the first rows. The bot reads the sheet through
``maia/pipeline/tools/store/google_sheets.py``.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from maia.models.tenants import TenantDocument
from maia.models.users import User
from maia.api.security import authenticate_user_token
from maia.services.google_sheets import (
    SheetAccessError,
    extract_sheet_id,
    read_sheet,
    service_account_email,
)

sheets_router = APIRouter(prefix="/sheets", tags=["Google Sheets"])

PREVIEW_ROWS = 5


class SheetConnectRequest(BaseModel):
    """Body of PUT /sheets: a Google Sheets link or a spreadsheet id."""
    url: str


def _tenant_or_404(tenant_name: str) -> TenantDocument:
    """Load the tenant or raise 404."""
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def _payload(tenant: TenantDocument, use_cache: bool = True) -> dict[str, Any]:
    """Response of every sheets endpoint: sheet id and url, the service-account address to share with, a preview of
    the first rows or an error text.
    """
    sheet_id = tenant.google_sheet_id or ""
    payload: dict[str, Any] = {
        "sheet_id": sheet_id or None,
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit" if sheet_id else None,
        "service_account": service_account_email(),
        "preview": None,
        "error": None,
    }
    if not sheet_id:
        return payload
    try:
        data = read_sheet(sheet_id, use_cache=use_cache)
        payload["preview"] = {
            "title": data.title,
            "headers": data.headers,
            "rows": data.rows[:PREVIEW_ROWS],
            "row_count": len(data.rows),
        }
    except SheetAccessError as exc:
        payload["error"] = str(exc)
    except Exception as exc:  # keep the settings screen usable even when Google is down
        payload["error"] = f"Could not read the sheet: {exc}"
    return payload


@sheets_router.get("/{tenant_name}", summary="Connected sheet + preview")
async def get_sheet(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """The connected sheet of the tenant, with a preview."""
    return _payload(_tenant_or_404(tenant_name))


@sheets_router.put("/{tenant_name}", summary="Connect a sheet (link or id); verified by reading it")
async def connect_sheet(tenant_name: str, body: SheetConnectRequest, user: User = Depends(authenticate_user_token)):
    """Connect a sheet: extract its id, read it once to verify access, store the id on the tenant."""
    tenant = _tenant_or_404(tenant_name)
    sheet_id = extract_sheet_id(body.url)
    if not sheet_id:
        raise HTTPException(status_code=400, detail="Not a Google Sheets link or spreadsheet id")
    try:
        read_sheet(sheet_id, use_cache=False)
    except SheetAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tenant.update(google_sheet_id=sheet_id)
    tenant.reload()
    return _payload(tenant)


@sheets_router.delete("/{tenant_name}", summary="Disconnect the sheet")
async def disconnect_sheet(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """Clear the tenant's sheet id."""
    tenant = _tenant_or_404(tenant_name)
    tenant.update(google_sheet_id="")
    tenant.reload()
    return _payload(tenant)
