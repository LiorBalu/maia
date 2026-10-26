"""Admin API: the tenant's customers (end users): switch a customer between AI and manual reply mode."""

import logging
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from maia.models.users import User
from maia.api.security import authenticate_user_token
from maia.utils.CustomJSONResponse import CustomJSONResponse
from maia.models.customers import Customer, CustomerDocument

logger = logging.getLogger(__name__)

customer_router = APIRouter(prefix="/customers", tags=["Customers/Clients"])


class CustomerStatusUpdateRequest(BaseModel):
    reply_mode: str


@customer_router.put("/{tenant_name}/{phone_number}/reply_mode", response_model=Customer)
async def update_customer_status(
    reply_mode: CustomerStatusUpdateRequest,
    tenant_name: str,
    phone_number: str,
    user: User = Depends(authenticate_user_token),
):
    customer_doc = CustomerDocument.objects(phone_number=phone_number, tenant_name=tenant_name).first()
    if not customer_doc:
        return JSONResponse(status_code=404, content={"message": "Customer not found"})

    customer_doc.reply_mode = reply_mode.reply_mode
    if reply_mode.reply_mode == "AI":
        customer_doc.agent_requested = False
    customer_doc.save()
    customer_doc.reload()
    return CustomJSONResponse(content=customer_doc.to_mongo().to_dict())
