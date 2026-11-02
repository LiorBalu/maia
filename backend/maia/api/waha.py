"""Admin API: a tenant's WhatsApp numbers on WAHA: create the session, QR / pairing code, status, disconnect."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel

from maia.models.tenants import TenantDocument, WahaAccount
from maia.models.users import User
from maia.api.security import authenticate_user_token
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


@router.get("/{tenant_name}/accounts", response_model=List[WahaAccountResponse])
async def get_waha_accounts(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """Get all WAHA accounts for a tenant"""
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return [
        WahaAccountResponse(
            phone_number=account.phone_number,
            webhook_url=account.webhook_url,
            is_connected=account.is_connected,
            session_status=(account.session_status if hasattr(account, "session_status") else "STOPPED"),
            last_status_update=(
                account.last_status_update.isoformat()
                if hasattr(account, "last_status_update") and account.last_status_update
                else None
            ),
        )
        for account in tenant.waha_accounts
    ]


@router.post("/{tenant_name}/accounts", response_model=WahaAccountResponse)
async def create_waha_account(
    tenant_name: str, request: WahaAccountRequest, user: User = Depends(authenticate_user_token)
):
    """Create a new WAHA account for a tenant"""
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Check if phone_number already exists
    for account in tenant.waha_accounts:
        if account.phone_number.replace("+", "") == request.phone_number.replace("+", ""):
            raise HTTPException(status_code=400, detail="Phone number already exists")

    # Create new account
    new_account = WahaAccount(phone_number=request.phone_number, webhook_url=request.webhook_url, is_connected=False)

    tenant.waha_accounts.append(new_account)
    tenant.save()

    return WahaAccountResponse(
        phone_number=new_account.phone_number,
        webhook_url=new_account.webhook_url,
        is_connected=new_account.is_connected,
        session_status="STOPPED",
        last_status_update=None,
    )


@router.delete("/{tenant_name}/accounts/{phone_number}")
async def delete_waha_account(tenant_name: str, phone_number: str, user: User = Depends(authenticate_user_token)):
    """Delete a WAHA account"""
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Find and remove the account
    account_to_remove = None
    for account in tenant.waha_accounts:
        if account.phone_number.replace("+", "") == phone_number.replace("+", ""):
            account_to_remove = account
            break

    if not account_to_remove:
        raise HTTPException(status_code=404, detail="WAHA account not found")

    # Try to logout and stop the session in WAHA before removing from database
    try:
        service = WahaService(tenant_name, phone_number)

        # First logout the session
        try:
            service.disconnect_client()
        except Exception as logout_error:
            logger.error(f"Warning: Failed to logout session: {logout_error}")

        # Then stop/delete the session
        try:
            service.stop_session()
        except Exception as stop_error:
            logger.error(f"Warning: Failed to stop session: {stop_error}")
    except Exception as e:
        # Log the error but continue with deletion from database
        logger.error(f"Warning: Failed to cleanup WAHA session: {e}")

    # Remove account from database
    tenant.waha_accounts.remove(account_to_remove)
    tenant.save()

    return {"message": "WAHA account deleted successfully"}


# The two calls below may wait several seconds on WAHA (session restart, QR), so they are plain `def`:
# FastAPI runs them in a worker thread and the event loop keeps serving the WAHA webhooks meanwhile.
@router.post("/{tenant_name}/accounts/{phone_number}/create-client")
def create_whatsapp_client(tenant_name: str, phone_number: str, user: User = Depends(authenticate_user_token)):
    """Create the WAHA session of the number with this tenant's webhook (re-pointing an existing one) and start it."""
    service = WahaService(tenant_name, phone_number)
    return service.create_whatsapp_client()


@router.get("/{tenant_name}/accounts/{phone_number}/qr")
def get_whatsapp_qr(tenant_name: str, phone_number: str, user: User = Depends(authenticate_user_token)):
    """QR to scan from the phone; (re)starts a missing, STOPPED or FAILED session first. 409 when already connected."""
    service = WahaService(tenant_name, phone_number)
    return service.get_qr_code()


@router.get("/{tenant_name}/accounts/{phone_number}/status")
async def get_client_status(tenant_name: str, phone_number: str, user: User = Depends(authenticate_user_token)):
    """Get WhatsApp session status from WAHA Pro"""
    service = WahaService(tenant_name, phone_number)
    return service.get_client_status()


@router.post("/{tenant_name}/accounts/{phone_number}/disconnect")
async def disconnect_client(tenant_name: str, phone_number: str, user: User = Depends(authenticate_user_token)):
    """Disconnect/logout WhatsApp session from WAHA Pro"""
    service = WahaService(tenant_name, phone_number)
    return service.disconnect_client()
