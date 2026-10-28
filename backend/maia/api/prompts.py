"""Admin API: the tenant's prompts (`default`, `platform_realtime`, named extras) and their edit history."""

import logging
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, HTTPException, Depends

from maia.services.registry import chatbot_for
from maia.models.prompts import Prompt, PromptDocument, PromptHistory, PromptHistoryModel, PromptPart
from maia.models.users import User
from maia.api.security import authenticate_user_token

logger = logging.getLogger(__name__)

prompts_router = APIRouter(prefix="/prompts", tags=["Prompts"])


@prompts_router.get("/history/{tenant_name}", response_model=List[PromptHistoryModel])
async def get_prompts_history(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """Every saved version of the tenant's prompts (PromptHistory)."""
    prompts = PromptHistory.objects(tenant_name=tenant_name)

    return [prompt.to_dict() for prompt in prompts]


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
        folders_for_tags=prompt.folders_for_tags,
        modified_at=datetime.now(timezone.utc),
        modified_by=user.email,
        prompt_type=prompt.prompt_type,
        prompt_parts=[PromptPart(**part.model_dump()) for part in prompt.prompt_parts],  # Convert to PromptPart objects
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
    - If prompt_type is not provided, it will be set to "text"
    - If the prompt type is "text", the content field will be updated
    - If the prompt type is not "text", the prompt_parts field will be updated
    """
    prompt_doc = PromptDocument.objects(name=name, tenant_name=tenant_name).first()
    if not prompt_doc:
        raise HTTPException(status_code=404, detail="Prompt not found")
    is_prompt_text = prompt_doc.prompt_type == "text"
    if prompt.prompt_type:
        if prompt.prompt_type != "text":
            is_prompt_text = False
    if is_prompt_text:
        prompt_doc.content = prompt.content or prompt_doc.content
    else:
        parts: List[PromptPart] = []
        chatbot_service = chatbot_for(tenant_name)
        # Create embedding vectors for each part
        for part in prompt.prompt_parts:
            try:
                part.embedding_vector = chatbot_service.get_embedding_vector(part.category + " " + part.content)
            except Exception as e:
                logger.error(f"Error creating embedding vector: {e}")
            parts.append(PromptPart(**part.model_dump()))
        prompt_doc.prompt_parts = parts
    prompt_doc.prompt_type = prompt.prompt_type or prompt_doc.prompt_type
    prompt_doc.label = prompt.label or prompt_doc.label
    prompt_doc.modified_by = user.email
    prompt_doc.folders_for_tags = prompt.folders_for_tags
    prompt_doc.modified_at = datetime.now(timezone.utc)
    prompt_doc.max_embedding_filter_parts = prompt.max_embedding_filter_parts or prompt_doc.max_embedding_filter_parts
    prompt_doc.save()
    prompt_doc.reload()
    return prompt_doc.to_dict()
