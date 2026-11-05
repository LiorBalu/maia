"""Admin API: the tenant's agent configuration (LLM provider, model, temperature, retrieval top-k)."""

# u: UserDocument = Security(authorize_current_user, scopes=["staff"])
from fastapi import APIRouter, HTTPException, Security

from maia.models.agents import AgentCreateRequest, AgentDocument, AgentResponse
from maia.api.security import authorize_current_user

agents_router = APIRouter(prefix="/agents", tags=["Agents"])


def _build_agent_response(agent_doc: AgentDocument) -> AgentResponse:
    """The API view of a stored agent, with created_at / updated_at as Unix seconds."""
    return AgentResponse(
        id=str(agent_doc.id),
        tenant_name=agent_doc.tenant_name,
        name=agent_doc.name,
        description=agent_doc.description,
        is_active=agent_doc.is_active,
        model=agent_doc.model,
        provider=agent_doc.provider,
        score_threshold=agent_doc.score_threshold,
        top_k_general=agent_doc.top_k_general,
        top_k_qa=agent_doc.top_k_qa,
        temperature=agent_doc.temperature,
        created_at=int(agent_doc.created_at.timestamp()),
        updated_at=int(agent_doc.updated_at.timestamp()),
    )


@agents_router.get("/", response_model=list[AgentResponse])
async def list_tenant_agents(
    tenant_name: str,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """List the tenant's agents (LLM configurations), newest first."""
    agent_docs = AgentDocument.objects(tenant_name=tenant_name).order_by("-created_at")
    return [_build_agent_response(agent_doc) for agent_doc in agent_docs]


def _verify_agent_name_available(tenant_name: str, agent_name: str) -> None:
    """Raise 400 when the tenant already has an agent with this name."""
    same_name_agent_doc = AgentDocument.objects(tenant_name=tenant_name, name=agent_name).first()
    if same_name_agent_doc:
        raise HTTPException(status_code=400, detail="An agent with this name already exists.")


def _build_agent_doc_from_payload(tenant_name: str, agent_request: AgentCreateRequest) -> AgentDocument:
    """A new, unsaved agent of the tenant with the request's fields (the caller saves it)."""
    return AgentDocument(
        tenant_name=tenant_name,
        name=agent_request.name,
        description=agent_request.description,
        is_active=agent_request.is_active,
        model=agent_request.model,
        provider=agent_request.provider,
        temperature=agent_request.temperature,
        score_threshold=agent_request.score_threshold,
        top_k_general=agent_request.top_k_general,
        top_k_qa=agent_request.top_k_qa,
    )


@agents_router.post("/", response_model=AgentResponse)
async def create_tenant_agent(
    tenant_name: str,
    agent_request: AgentCreateRequest,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """Create an agent for the tenant; the name must be unique. Saving an active agent deactivates the others."""
    _verify_agent_name_available(tenant_name, agent_request.name)

    new_agent_doc = _build_agent_doc_from_payload(tenant_name, agent_request)
    new_agent_doc.save()
    return _build_agent_response(new_agent_doc)


# duplicate an agent


def _load_agent_doc_or_404(agent_id: str) -> AgentDocument:
    """Load the agent by id or raise 404."""
    agent_doc = AgentDocument.objects(id=agent_id).first()
    if not agent_doc:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return agent_doc


def _apply_payload_to_agent_doc(agent_doc: AgentDocument, agent_request: AgentCreateRequest) -> None:
    """Overwrite the agent's fields with the request's values, in place (the caller saves)."""
    agent_doc.name = agent_request.name
    agent_doc.description = agent_request.description
    agent_doc.is_active = agent_request.is_active
    agent_doc.model = agent_request.model
    agent_doc.provider = agent_request.provider
    agent_doc.temperature = agent_request.temperature
    agent_doc.score_threshold = agent_request.score_threshold
    agent_doc.top_k_general = agent_request.top_k_general
    agent_doc.top_k_qa = agent_request.top_k_qa


@agents_router.put("/{agent_id}", response_model=AgentResponse)
async def update_tenant_agent(
    agent_id: str,
    agent_request: AgentCreateRequest,
    current_user: dict = Security(authorize_current_user, scopes=["staff"]),
):
    """Replace every field of an existing agent with the request values."""
    agent_doc = _load_agent_doc_or_404(agent_id)

    _apply_payload_to_agent_doc(agent_doc, agent_request)
    agent_doc.save()

    return _build_agent_response(agent_doc)
