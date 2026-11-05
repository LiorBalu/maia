"""Admin API: a tenant's WhatsApp numbers on WAHA: create the session, QR / pairing code, status, disconnect."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel

from maia.models.tenants import TenantDocument, WahaAccount
from maia.models.users import User
from maia.api.security import authenticate_current_user
from maia.services.waha import WahaService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/waha", tags=["WAHA"])


class WahaAccountRequest(BaseModel):
    """Body of account creation: the WhatsApp number and an optional webhook URL."""
    phone_number: str
    webhook_url: Optional[str] = None


class WahaAccountResponse(BaseModel):
    """A WAHA account as the dashboard sees it: number and connection status."""
    phone_number: str
    webhook_url: Optional[str] = None
    is_connected: bool = False
    session_status: Optional[str] = "STOPPED"  # Could be "STOPPED", "STARTING", "SCAN_QR_CODE", "WORKING", "FAILED"
    last_status_update: Optional[str] = None


# ====== CRUD Operations ======


def _load_tenant_or_404(tenant_name: str) -> TenantDocument:
    """Load the tenant or raise 404."""
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant_doc


def _build_waha_account_response(waha_account: WahaAccount) -> WahaAccountResponse:
    """The dashboard view of a stored WAHA account ('STOPPED' and no update time when the status fields are missing)."""
    return WahaAccountResponse(
        phone_number=waha_account.phone_number,
        webhook_url=waha_account.webhook_url,
        is_connected=waha_account.is_connected,
        session_status=(waha_account.session_status if hasattr(waha_account, "session_status") else "STOPPED"),
        last_status_update=(
            waha_account.last_status_update.isoformat()
            if hasattr(waha_account, "last_status_update") and waha_account.last_status_update
            else None
        ),
    )


@router.get("/{tenant_name}/accounts", response_model=List[WahaAccountResponse])
async def list_tenant_waha_accounts(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """Get all WAHA accounts for a tenant"""
    tenant_doc = _load_tenant_or_404(tenant_name)

    return [_build_waha_account_response(waha_account) for waha_account in tenant_doc.waha_accounts]


def _find_waha_account_by_number(tenant_doc: TenantDocument, phone_number: str) -> Optional[WahaAccount]:
    """The tenant's first WAHA account whose number equals phone_number when '+' is ignored; None when none does."""
    for candidate_account in tenant_doc.waha_accounts:
        if candidate_account.phone_number.replace("+", "") == phone_number.replace("+", ""):
            return candidate_account
    return None


@router.post("/{tenant_name}/accounts", response_model=WahaAccountResponse)
async def create_tenant_waha_account(
    tenant_name: str, waha_account_request: WahaAccountRequest, current_user: User = Depends(authenticate_current_user)
):
    """Create a new WAHA account for a tenant"""
    tenant_doc = _load_tenant_or_404(tenant_name)

    # Check if phone_number already exists
    if _find_waha_account_by_number(tenant_doc, waha_account_request.phone_number) is not None:
        raise HTTPException(status_code=400, detail="Phone number already exists")

    # Create new account
    new_waha_account = WahaAccount(phone_number=waha_account_request.phone_number, webhook_url=waha_account_request.webhook_url, is_connected=False)

    tenant_doc.waha_accounts.append(new_waha_account)
    tenant_doc.save()

    return _build_waha_account_response(new_waha_account)


def _logout_and_stop_waha_session(tenant_name: str, phone_number: str) -> None:
    """Best effort: log the number's WAHA session out, then stop it. Every failure, building the service included, is
    only logged, so the caller can still remove the account from the database.
    """
    try:
        waha_service = WahaService(tenant_name, phone_number)

        # First logout the session
        try:
            waha_service.logout_session()
        except Exception as logout_error:
            logger.error(f"Warning: Failed to logout session: {logout_error}")

        # Then stop/delete the session
        try:
            waha_service.stop_session_and_record_status()
        except Exception as stop_error:
            logger.error(f"Warning: Failed to stop session: {stop_error}")
    except Exception as cleanup_error:
        # Log the error but continue with deletion from database
        logger.error(f"Warning: Failed to cleanup WAHA session: {cleanup_error}")


@router.delete("/{tenant_name}/accounts/{phone_number}")
async def delete_tenant_waha_account(tenant_name: str, phone_number: str, current_user: User = Depends(authenticate_current_user)):
    """Delete a WAHA account"""
    tenant_doc = _load_tenant_or_404(tenant_name)

    # Find and remove the account
    matching_account = _find_waha_account_by_number(tenant_doc, phone_number)
    if matching_account is None:
        raise HTTPException(status_code=404, detail="WAHA account not found")

    # Try to logout and stop the session in WAHA before removing from database
    _logout_and_stop_waha_session(tenant_name, phone_number)

    # Remove account from database
    tenant_doc.waha_accounts.remove(matching_account)
    tenant_doc.save()

    return {"message": "WAHA account deleted successfully"}


# The two calls below may wait several seconds on WAHA (session restart, QR), so they are plain `def`:
# FastAPI runs them in a worker thread and the event loop keeps serving the WAHA webhooks meanwhile.
@router.post("/{tenant_name}/accounts/{phone_number}/create-client")
def provision_waha_session(tenant_name: str, phone_number: str, current_user: User = Depends(authenticate_current_user)):
    """Create the WAHA session of the number with this tenant's webhook (re-pointing an existing one) and start it."""
    waha_service = WahaService(tenant_name, phone_number)
    return waha_service.provision_session()


@router.get("/{tenant_name}/accounts/{phone_number}/qr")
def fetch_waha_qr_code(tenant_name: str, phone_number: str, current_user: User = Depends(authenticate_current_user)):
    """QR to scan from the phone; (re)starts a missing, STOPPED or FAILED session first. 409 when already connected."""
    waha_service = WahaService(tenant_name, phone_number)
    return waha_service.fetch_qr_code_when_ready()


@router.get("/{tenant_name}/accounts/{phone_number}/status")
async def sync_waha_session_status(tenant_name: str, phone_number: str, current_user: User = Depends(authenticate_current_user)):
    """Get WhatsApp session status from WAHA Pro"""
    waha_service = WahaService(tenant_name, phone_number)
    return waha_service.sync_session_status()


@router.post("/{tenant_name}/accounts/{phone_number}/disconnect")
async def logout_waha_session(tenant_name: str, phone_number: str, current_user: User = Depends(authenticate_current_user)):
    """Disconnect/logout WhatsApp session from WAHA Pro"""
    waha_service = WahaService(tenant_name, phone_number)
    return waha_service.logout_session()
