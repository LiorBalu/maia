"""Admin API: the tenant's prompts (`default`, `platform_realtime`, named extras)."""

import logging
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, HTTPException, Depends

from maia.models.prompts import Prompt, PromptDocument
from maia.models.users import User
from maia.api.security import authenticate_user_token

logger = logging.getLogger(__name__)

prompts_router = APIRouter(prefix="/prompts", tags=["Prompts"])


@prompts_router.post("", response_model=Prompt)
async def create_prompt(prompt: Prompt, user: User = Depends(authenticate_user_token)):
    """Create a prompt for a tenant; the name must be unique within the tenant."""
    if prompt.name == "":
        raise HTTPException(status_code=400, detail="Prompt name are required")
    if PromptDocument.objects(name=prompt.name, tenant_name=prompt.tenant_name).first():
        raise HTTPException(status_code=400, detail="Prompt name already exists")
    prompt_doc = PromptDocument(
        name=prompt.name,
        tenant_name=prompt.tenant_name,
        content=prompt.content,
        modified_at=datetime.now(timezone.utc),
        modified_by=user.email,
        label=prompt.label,
    )
    prompt_doc.save()
    return prompt_doc.to_dict()


@prompts_router.get("/{tenant_name}", response_model=List[Prompt])
async def get_prompts(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """All prompts of the tenant."""
    prompts = PromptDocument.objects(tenant_name=tenant_name)
    return [prompt.to_dict() for prompt in prompts]


@prompts_router.put("/{tenant_name}/{name}", response_model=Prompt)
async def update_prompt(tenant_name: str, prompt: Prompt, name: str, user: User = Depends(authenticate_user_token)):
    """
    ### Update a prompt in the database.
    - The field name is not updated in the database
    - `content` and `label` are replaced when given (an empty value keeps the stored one)
    """
    prompt_doc = PromptDocument.objects(name=name, tenant_name=tenant_name).first()
    if not prompt_doc:
        raise HTTPException(status_code=404, detail="Prompt not found")
    prompt_doc.content = prompt.content or prompt_doc.content
    prompt_doc.label = prompt.label or prompt_doc.label
    prompt_doc.modified_by = user.email
    prompt_doc.modified_at = datetime.now(timezone.utc)
    prompt_doc.save()
    prompt_doc.reload()
    return prompt_doc.to_dict()
