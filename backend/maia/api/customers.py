"""Admin API: the tenant's customers (end users): switch a customer between AI and manual reply mode."""

import logging
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from maia.models.users import User
from maia.api.security import authenticate_current_user
from maia.utils.CustomJSONResponse import CustomJSONResponse
from maia.models.customers import CustomerDocument

logger = logging.getLogger(__name__)

customers_router = APIRouter(prefix="/customers", tags=["Customers/Clients"])


class CustomerStatusUpdateRequest(BaseModel):
    """Body of the reply_mode update: AI, agent or blocked."""
    reply_mode: str


@customers_router.put("/{tenant_name}/{phone_number}/reply_mode")
async def update_customer_reply_mode(
    reply_mode_update: CustomerStatusUpdateRequest,
    tenant_name: str,
    phone_number: str,
    current_user: User = Depends(authenticate_current_user),
):
    """Switch a customer between AI replies and manual handling; returns the stored customer."""
    customer_record = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    if not customer_record:
        return JSONResponse(status_code=404, content={"message": "Customer not found"})

    customer_record.reply_mode = reply_mode_update.reply_mode
    customer_record.save()
    customer_record.reload()
    return CustomJSONResponse(content=customer_record.to_mongo().to_dict())
