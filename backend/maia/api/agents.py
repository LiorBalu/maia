"""Admin API: the tenant's agent configuration (LLM provider, model, temperature, retrieval top-k)."""

# u: UserDocument = Security(verify_token_extended, scopes=["staff"])
from fastapi import APIRouter, HTTPException, Security

from maia.models.agents import AgentCreateRequest, AgentDocument, AgentResponse
from maia.api.security import verify_token_extended

agents_router = APIRouter(prefix="/agents", tags=["Agents"])


@agents_router.get("/", response_model=list[AgentResponse])
async def get_agents(
    tenant_name: str,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    agents = AgentDocument.objects(tenant_name=tenant_name).order_by("-created_at")
    return [
        AgentResponse(
            id=str(agent.id),
            tenant_name=agent.tenant_name,
            name=agent.name,
            description=agent.description,
            is_active=agent.is_active,
            model=agent.model,
            provider=agent.provider,
            score_threshold=agent.score_threshold,
            top_k_general=agent.top_k_general,
            top_k_qa=agent.top_k_qa,
            direct_answer_threshold=agent.direct_answer_threshold or 0,
            temperature=agent.temperature,
            created_at=int(agent.created_at.timestamp()),
            updated_at=int(agent.updated_at.timestamp()),
        )
        for agent in agents
    ]


@agents_router.post("/", response_model=AgentResponse)
async def create_agent(
    tenant_name: str,
    request: AgentCreateRequest,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    existing = AgentDocument.objects(tenant_name=tenant_name, name=request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="An agent with this name already exists.")

    doc = AgentDocument(
        tenant_name=tenant_name,
        name=request.name,
        description=request.description,
        is_active=request.is_active,
        model=request.model,
        provider=request.provider,
        temperature=request.temperature,
        score_threshold=request.score_threshold,
        top_k_general=request.top_k_general,
        top_k_qa=request.top_k_qa,
        direct_answer_threshold=request.direct_answer_threshold,
    )
    doc.save()
    return AgentResponse(
        id=str(doc.id),
        tenant_name=doc.tenant_name,
        name=doc.name,
        description=doc.description,
        is_active=doc.is_active,
        model=doc.model,
        provider=doc.provider,
        score_threshold=doc.score_threshold,
        top_k_general=doc.top_k_general,
        top_k_qa=doc.top_k_qa,
        direct_answer_threshold=doc.direct_answer_threshold or 0,
        temperature=doc.temperature,
        created_at=int(doc.created_at.timestamp()),
        updated_at=int(doc.updated_at.timestamp()),
    )


# duplicate an agent


@agents_router.put("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    request: AgentCreateRequest,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    agent = AgentDocument.objects(id=agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    agent.name = request.name
    agent.description = request.description
    agent.is_active = request.is_active
    agent.model = request.model
    agent.provider = request.provider
    agent.temperature = request.temperature
    agent.score_threshold = request.score_threshold
    agent.top_k_general = request.top_k_general
    agent.top_k_qa = request.top_k_qa
    agent.direct_answer_threshold = request.direct_answer_threshold
    agent.save()

    return AgentResponse(
        id=str(agent.id),
        tenant_name=agent.tenant_name,
        name=agent.name,
        description=agent.description,
        is_active=agent.is_active,
        model=agent.model,
        score_threshold=agent.score_threshold,
        top_k_general=agent.top_k_general,
        top_k_qa=agent.top_k_qa,
        direct_answer_threshold=agent.direct_answer_threshold or 0,
        provider=agent.provider,
        temperature=agent.temperature,
        created_at=int(agent.created_at.timestamp()),
        updated_at=int(agent.updated_at.timestamp()),
    )


@agents_router.delete("/{agent_id}")
async def delete_agent(
    agent_id: str,
    user: dict = Security(verify_token_extended, scopes=["staff"]),
):
    agent = AgentDocument.objects(id=agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    agent.delete()
    return {"detail": "Agent deleted successfully."}
