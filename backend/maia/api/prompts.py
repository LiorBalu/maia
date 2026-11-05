"""Admin API: the tenant's prompts (`default`, `platform_realtime`, named extras)."""

import logging
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, HTTPException, Depends

from maia.models.prompts import Prompt, PromptDocument
from maia.models.users import User
from maia.api.security import authenticate_current_user

logger = logging.getLogger(__name__)

prompts_router = APIRouter(prefix="/prompts", tags=["Prompts"])


def _verify_new_prompt_name(tenant_name: str, prompt_name: str) -> None:
    """Raise 400 when the name is empty or the tenant already has a prompt with this name."""
    if prompt_name == "":
        raise HTTPException(status_code=400, detail="Prompt name are required")
    if PromptDocument.objects(name=prompt_name, tenant_name=tenant_name).first():
        raise HTTPException(status_code=400, detail="Prompt name already exists")


def _persist_new_prompt_doc(prompt_payload: Prompt, editor_email: str) -> PromptDocument:
    """Save the payload as a new prompt of its tenant, stamped with the editor and the current time."""
    new_prompt_doc = PromptDocument(
        name=prompt_payload.name,
        tenant_name=prompt_payload.tenant_name,
        content=prompt_payload.content,
        modified_at=datetime.now(timezone.utc),
        modified_by=editor_email,
        label=prompt_payload.label,
    )
    new_prompt_doc.save()
    return new_prompt_doc


@prompts_router.post("", response_model=Prompt)
async def create_tenant_prompt(prompt_payload: Prompt, current_user: User = Depends(authenticate_current_user)):
    """Create a prompt for a tenant; the name must be unique within the tenant."""
    _verify_new_prompt_name(tenant_name=prompt_payload.tenant_name, prompt_name=prompt_payload.name)
    created_prompt_doc = _persist_new_prompt_doc(prompt_payload, current_user.email)
    return created_prompt_doc.serialize_for_api()


@prompts_router.get("/{tenant_name}", response_model=List[Prompt])
async def list_tenant_prompts(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """All prompts of the tenant."""
    tenant_prompt_docs = PromptDocument.objects(tenant_name=tenant_name)
    return [prompt_doc.serialize_for_api() for prompt_doc in tenant_prompt_docs]


def _load_tenant_prompt_or_404(tenant_name: str, prompt_name: str) -> PromptDocument:
    """Load the tenant's prompt by name or raise 404."""
    stored_prompt_doc = PromptDocument.objects(name=prompt_name, tenant_name=tenant_name).first()
    if not stored_prompt_doc:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return stored_prompt_doc


def _persist_prompt_edit(prompt_doc: PromptDocument, prompt_payload: Prompt, editor_email: str) -> PromptDocument:
    """Overwrite content and label when the payload gives them (an empty value keeps the stored one), stamp the
    editor and the time, then save and reload the prompt.
    """
    prompt_doc.content = prompt_payload.content or prompt_doc.content
    prompt_doc.label = prompt_payload.label or prompt_doc.label
    prompt_doc.modified_by = editor_email
    prompt_doc.modified_at = datetime.now(timezone.utc)
    prompt_doc.save()
    prompt_doc.reload()
    return prompt_doc


@prompts_router.put("/{tenant_name}/{name}", response_model=Prompt)
async def update_tenant_prompt(tenant_name: str, prompt_payload: Prompt, name: str, current_user: User = Depends(authenticate_current_user)):
    """
    ### Update a prompt in the database.
    - The field name is not updated in the database
    - `content` and `label` are replaced when given (an empty value keeps the stored one)
    """
    stored_prompt_doc = _load_tenant_prompt_or_404(tenant_name=tenant_name, prompt_name=name)
    stored_prompt_doc = _persist_prompt_edit(stored_prompt_doc, prompt_payload, current_user.email)
    return stored_prompt_doc.serialize_for_api()
