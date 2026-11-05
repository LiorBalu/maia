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
from maia.api.security import authenticate_current_user
from maia.services.google_sheets import (
    SheetAccessError,
    SheetData,
    resolve_sheet_id,
    fetch_sheet_data,
    load_service_account_email,
)

sheets_router = APIRouter(prefix="/sheets", tags=["Google Sheets"])

PREVIEW_ROW_LIMIT = 5


class SheetConnectRequest(BaseModel):
    """Body of PUT /sheets: a Google Sheets link or a spreadsheet id."""
    url: str


def _load_tenant_or_404(tenant_name: str) -> TenantDocument:
    """Load the tenant or raise 404."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant_doc


def _build_sheet_preview(sheet_data: SheetData) -> dict[str, Any]:
    """The preview of the response: title, headers, the first PREVIEW_ROW_LIMIT rows and the number of rows."""
    return {
        "title": sheet_data.spreadsheet_title,
        "headers": sheet_data.column_headers,
        "rows": sheet_data.data_rows[:PREVIEW_ROW_LIMIT],
        "row_count": len(sheet_data.data_rows),
    }


def _fetch_sheet_preview_or_error(sheet_id: str, should_use_cache: bool) -> tuple[dict[str, Any] | None, str | None]:
    """Read the sheet: (preview, None), or (None, error text) when reading or shaping it raises an Exception."""
    try:
        sheet_data = fetch_sheet_data(sheet_id, should_use_cache=should_use_cache)
        return _build_sheet_preview(sheet_data), None
    except SheetAccessError as access_error:
        return None, str(access_error)
    except Exception as read_error:  # keep the settings screen usable even when Google is down
        return None, f"Could not read the sheet: {read_error}"


def _build_sheet_connection_response(tenant_doc: TenantDocument, should_use_cache: bool = True) -> dict[str, Any]:
    """Response of every sheets endpoint: sheet id and url, the service-account address to share with, a preview of
    the first rows or an error text.
    """
    connected_sheet_id = tenant_doc.google_sheet_id or ""
    sheet_connection: dict[str, Any] = {
        "sheet_id": connected_sheet_id or None,
        "sheet_url": (
            f"https://docs.google.com/spreadsheets/d/{connected_sheet_id}/edit" if connected_sheet_id else None
        ),
        "service_account": load_service_account_email(),
        "preview": None,
        "error": None,
    }
    if not connected_sheet_id:
        return sheet_connection
    sheet_connection["preview"], sheet_connection["error"] = _fetch_sheet_preview_or_error(
        connected_sheet_id, should_use_cache
    )
    return sheet_connection


@sheets_router.get("/{tenant_name}", summary="Connected sheet + preview")
async def fetch_sheet_connection(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """The connected sheet of the tenant, with a preview."""
    return _build_sheet_connection_response(_load_tenant_or_404(tenant_name))


def _resolve_verified_sheet_id(sheet_link_or_id: str) -> str:
    """The spreadsheet id of a link or id, once an uncached read proved the service account can open the sheet.
    400 when it is neither a link nor an id, and 400 with the SheetAccessError text when the read is refused.
    """
    extracted_sheet_id = resolve_sheet_id(sheet_link_or_id)
    if not extracted_sheet_id:
        raise HTTPException(status_code=400, detail="Not a Google Sheets link or spreadsheet id")
    try:
        fetch_sheet_data(extracted_sheet_id, should_use_cache=False)
    except SheetAccessError as access_error:
        raise HTTPException(status_code=400, detail=str(access_error)) from access_error
    return extracted_sheet_id


def _persist_sheet_id(tenant_doc: TenantDocument, sheet_id: str) -> TenantDocument:
    """Store the sheet id on the tenant ('' disconnects the sheet) and return the reloaded tenant."""
    tenant_doc.update(google_sheet_id=sheet_id)
    tenant_doc.reload()
    return tenant_doc


@sheets_router.put("/{tenant_name}", summary="Connect a sheet (link or id); verified by reading it")
async def connect_sheet(tenant_name: str, connect_request: SheetConnectRequest, current_user: User = Depends(authenticate_current_user)):
    """Connect a sheet: extract its id, read it once to verify access, store the id on the tenant."""
    tenant_doc = _load_tenant_or_404(tenant_name)
    verified_sheet_id = _resolve_verified_sheet_id(connect_request.url)
    tenant_doc = _persist_sheet_id(tenant_doc, verified_sheet_id)
    return _build_sheet_connection_response(tenant_doc)


@sheets_router.delete("/{tenant_name}", summary="Disconnect the sheet")
async def disconnect_sheet(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """Clear the tenant's sheet id."""
    tenant_doc = _load_tenant_or_404(tenant_name)
    tenant_doc = _persist_sheet_id(tenant_doc, "")
    return _build_sheet_connection_response(tenant_doc)
