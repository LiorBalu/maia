"""Admin API: tenants (businesses): list, create with defaults, detail (masked secrets), update, ownership."""

import logging
import os
import re
from typing import List, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from maia.services.registry import refresh_cached_tenant_doc
from maia.models.tenants import AnthropicSettings, AnthropicSettingsModel, GoogleLLMSettings, GoogleLLMSettingsModel, OpenAISettings, OpenAISettingsModel, WahaAccountModel, TenantCustomer, TenantDocument, TwilioAccount, TwilioAccountModel
from maia.models.users import User, UserDocument
from maia.models.settings import CallSettingsDocument, ChatSettingsDocument
from maia.models.tenant_features import FeatureTenant, FeatureTenantModel
from maia.api.security import authorize_current_user
from maia.api.auth import verify_email_unregistered, enroll_member_in_tenant, verify_password_meets_policy
from maia.pipeline.tools.function_decorator import FunctionTool
from maia.utils.make_automation import seed_default_prompt

logger = logging.getLogger(__name__)

TENANT_DEFAULT_TIMEZONE = "Asia/Jerusalem"


def ensure_chat_and_call_settings(tenant_name: str) -> None:
    """Create default ChatSettings and CallSettings documents for the tenant if missing."""
    if not ChatSettingsDocument.objects(tenant_name=tenant_name).first():
        chat_settings_doc = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings_doc.backfill_feature_defaults()
        chat_settings_doc.save()
    if not CallSettingsDocument.objects(tenant_name=tenant_name).first():
        CallSettingsDocument(tenant_name=tenant_name).save()


def extract_embedded_settings_fields(embedded_doc) -> dict:
    """
    Safely extract field values from an EmbeddedDocument using Python attribute names.
    This avoids the db_field mismatch issue that can occur with to_mongo().

    Args:
        embedded_doc: A mongoengine EmbeddedDocument instance

    Returns:
        dict: A dictionary with Python field names as keys
    """
    if not embedded_doc:
        return {}
    field_values = {}
    # returns the fields of the embedded document as a dictionary in python field names
    for field_name in embedded_doc._fields.keys():
        field_value = getattr(embedded_doc, field_name, None)
        if field_value is not None:
            field_values[field_name] = field_value
    return field_values


def merge_nested_dicts(base_values: dict, incoming_values: dict) -> dict:
    """
    Deep merge incoming_values into base_values, returning a new dict.
    Handles nested dictionaries recursively.

    Args:
        base_values: The base dictionary to merge into
        incoming_values: The dictionary with updates to apply

    Returns:
        dict: A new merged dictionary
    """
    merged_values = base_values.copy()
    for key, incoming_value in incoming_values.items():
        if incoming_value is None:
            # Skip None values to preserve existing settings
            continue
        if key in merged_values and isinstance(merged_values[key], dict) and isinstance(incoming_value, dict):
            merged_values[key] = merge_nested_dicts(merged_values[key], incoming_value)
        else:
            merged_values[key] = incoming_value
    return merged_values


def merge_embedded_settings_with_stored(stored_settings_doc, incoming_values: dict) -> dict:
    """
    Merge new values into existing embedded document settings.

    - Preserves existing fields not present in incoming_values
    - Updates fields present in incoming_values (with non-None values)
    - Uses deep merge for nested structures (future-proofing)

    Args:
        stored_settings_doc: The existing EmbeddedDocument instance
        incoming_values: Dictionary of new values to merge

    Returns:
        dict: Merged settings ready for EmbeddedDocument constructor
    """
    stored_values = extract_embedded_settings_fields(stored_settings_doc)
    return merge_nested_dicts(stored_values, incoming_values)


def _redact_api_key(api_key: str) -> str:
    """Return first char + '**' + last char (or just the value if shorter than 2)."""
    if not api_key:
        return api_key
    if len(api_key) < 2:
        return f"{api_key}**"
    return f"{api_key[0]}**{api_key[-1]}"


def _collect_shared_openai_api_keys() -> List[str]:
    """Non-empty shared OpenAI API keys from env vars."""
    return [api_key for api_key in (os.environ.get("MAIA_OPENAI_API_KEY"),) if api_key]


def redact_openai_api_key_if_shared(api_key: Optional[str]) -> Optional[str]:
    """If the key matches a shared env-var key, return its masked form. Otherwise return as-is."""
    if not api_key:
        return api_key
    if api_key in _collect_shared_openai_api_keys():
        return _redact_api_key(api_key)
    return api_key


def is_redacted_shared_openai_api_key(api_key: Optional[str]) -> bool:
    """True if the value is the masked form of a shared env-var key (i.e. a fake value sent back by the client)."""
    if not api_key:
        return False
    return api_key in {_redact_api_key(shared_api_key) for shared_api_key in _collect_shared_openai_api_keys()}


TENANT_NOT_FOUND_DETAIL = "Tenant not found"

tenants_router = APIRouter(prefix="/tenants", tags=["Tenants"])


class TenantRequest(BaseModel):
    """Body of tenant creation / update: status, LLM providers, channels, features, tools."""
    name: Optional[str] = None
    status: Literal["active", "inactive", "pending"] = "active"
    description: Optional[str] = None
    openai_api_key: Optional[str] = None
    gpt_model: Optional[str] = None
    access_users: Optional[List[str]] = None
    twilio_accounts: Optional[List[TwilioAccountModel]] = None
    manual_mode: Optional[bool] = False
    openai_settings: Optional[OpenAISettingsModel] = None
    anthropic_settings: Optional[AnthropicSettingsModel] = None
    google_llm_settings: Optional[GoogleLLMSettingsModel] = None
    features: List[FeatureTenantModel] = []
    default_language: Optional[str] = "he"
    default_timezone: Optional[str] = TENANT_DEFAULT_TIMEZONE
    owner_phone_number: Optional[str] = None
    # Names of the registered `@llm_tool` functions the bot may call (see GET /management/v1/tools)
    tools: Optional[List[str]] = None

    class Config:
        """Example payload shown in the API docs."""
        json_schema_extra = {
            "example": {
                "name": "tenant_name",
                "description": "Tenant description",
                "openai_api_key": "sldjhfgsdlkjhsdlkfjh",
                "gpt_model": "gpt-4o-mini",
                "access_users": ["tes@gmail.com"],
                "twilio_accounts": [
                    {
                        "phone_number": "+1234567890",
                        "account_sid": "account_sid",
                        "auth_token": "auth_token",
                    }
                ],
                "manual_mode": False,
            }
        }


class TenantCreateRequest(TenantRequest):
    """Body of POST / PUT: a new business may come with its owner's dashboard login (POST only; ignored on PUT).
    Kept out of `TenantRequest` so the list response never carries these fields."""
    owner_email: Optional[str] = None
    owner_password: Optional[str] = None


OWNER_LOGIN_FIELDS = ("owner_email", "owner_password")


@tenants_router.get("", response_model=List[TenantRequest])
async def list_accessible_tenants(current_user: UserDocument = Security(authorize_current_user, scopes=["staff"])):
    """
    Retrieve a list of tenants accessible to the user.

    If the user is a staff member, all tenants are retrieved. Otherwise, only tenants that the
    user has access to.

    Args:
        current_user (User): The user object, obtained through dependency injection
                             and token verification.

    Returns:
        List[Dict[str, str]]: A list of dictionaries, each containing the 'name'
                              and 'description' of a tenant.
    """
    if current_user.is_staff:
        tenant_docs = TenantDocument.objects().only("name", "description", "status")
    else:
        permitted_tenant_names = list(current_user.permissions.keys())
        tenant_docs = TenantDocument.objects(name__in=permitted_tenant_names).only("name", "description", "status")

    return [
        {"name": tenant_doc.name, "description": tenant_doc.description, "status": tenant_doc.status}
        for tenant_doc in tenant_docs
    ]


_LEAVE_FIELD_UNTOUCHED = object()


def _dedupe_and_verify_tool_names(tool_names: Optional[List[str]]) -> List[str]:
    """The tool names to store: deduplicated, every one registered at startup (400 otherwise)."""
    unique_tool_names = list(dict.fromkeys(tool_name for tool_name in (tool_names or []) if tool_name))
    unknown_tool_names = [tool_name for tool_name in unique_tool_names if not FunctionTool.find_tool(tool_name)]
    if unknown_tool_names:
        raise HTTPException(status_code=400, detail=f"Unknown tools: {', '.join(unknown_tool_names)}")
    return unique_tool_names


def _normalize_tenant_name(raw_name: Optional[str]) -> str:
    """The name of a new tenant, lower-cased with spaces turned into underscores; 400 when it is missing or holds
    anything other than letters, digits, _ and -."""
    if not raw_name:
        raise HTTPException(status_code=400, detail="Tenant name is required")
    normalized_name = raw_name.lower().replace(" ", "_")
    if not re.match("^[a-zA-Z0-9_-]+$", normalized_name):
        raise HTTPException(status_code=400, detail="The name must be alphanumeric and may contain digits, _, or -.")
    return normalized_name


def _verify_owner_login_fields(tenant_payload: TenantCreateRequest) -> None:
    """The owner login of a new tenant: email and password come together (400 otherwise); when given, the email
    must be free (its normalized form is written back on the payload) and the password must meet the policy."""
    if bool(tenant_payload.owner_email) != bool(tenant_payload.owner_password):
        raise HTTPException(status_code=400, detail="Owner email and password go together")
    if tenant_payload.owner_email:
        tenant_payload.owner_email = verify_email_unregistered(tenant_payload.owner_email)
        verify_password_meets_policy(tenant_payload.owner_password)


def _verify_tenant_creation_request(http_request: Request, tenant_payload: TenantCreateRequest, current_user: User) -> None:
    """Guard the POST (creation) flow: authorisation, tenant-name format, and the owner login when given
    (both fields together, free email, password policy) so that nothing is created when it is invalid."""
    if http_request.method != "POST":
        return
    if not current_user.is_staff:
        raise HTTPException(status_code=403, detail="You are not authorized to create a tenant")
    tenant_payload.name = _normalize_tenant_name(tenant_payload.name)
    _verify_owner_login_fields(tenant_payload)


def _resolve_embedded_settings_update(stored_settings_doc, incoming_value, settings_class):
    """Merge an embedded settings payload with what is stored, or skip an empty one."""
    if not incoming_value or (
        isinstance(incoming_value, dict) and not any(setting_value for setting_value in incoming_value.values())
    ):
        return _LEAVE_FIELD_UNTOUCHED
    if not isinstance(incoming_value, dict):
        return incoming_value
    return settings_class(**merge_embedded_settings_with_stored(stored_settings_doc, incoming_value))


def _drop_redacted_shared_openai_key(openai_settings_values: dict) -> None:
    """Remove ``openai_api_key`` from an incoming openai_settings dict, in place, when it holds the redacted form of
    a shared env-var key."""
    if is_redacted_shared_openai_api_key(openai_settings_values.get("openai_api_key")):
        # The client sent back the masked form of a shared env-var key — drop it so the
        # existing value in DB is preserved (avoids saving a fake masked key).
        openai_settings_values.pop("openai_api_key", None)


def _convert_update_field_value(tenant_doc: TenantDocument, field_name: str, incoming_value):
    """Convert one incoming field into its embedded-document form.

    Returns ``_LEAVE_FIELD_UNTOUCHED`` when the field must be left untouched on the document.
    """
    if field_name == "openai_settings":
        if isinstance(incoming_value, dict):
            _drop_redacted_shared_openai_key(incoming_value)
        return _resolve_embedded_settings_update(tenant_doc.openai_settings, incoming_value, OpenAISettings)
    if field_name == "anthropic_settings":
        return _resolve_embedded_settings_update(tenant_doc.anthropic_settings, incoming_value, AnthropicSettings)
    if field_name == "google_llm_settings":
        return _resolve_embedded_settings_update(tenant_doc.google_llm_settings, incoming_value, GoogleLLMSettings)
    if field_name == "tools":
        # An explicit list (even empty) replaces the stored one; null leaves it untouched.
        return _LEAVE_FIELD_UNTOUCHED if incoming_value is None else _dedupe_and_verify_tool_names(incoming_value)
    if not incoming_value:
        return incoming_value
    if field_name == "twilio_accounts":
        return [TwilioAccount(**account) for account in incoming_value]
    if field_name == "features":
        return [FeatureTenant(**feature) for feature in incoming_value]
    return incoming_value


def _apply_payload_to_tenant_doc(tenant_doc: TenantDocument, tenant_payload: TenantRequest) -> None:
    """Apply a partial update on an existing tenant."""
    incoming_fields = tenant_payload.dict(exclude_unset=True, exclude=set(OWNER_LOGIN_FIELDS))
    for field_name, incoming_value in incoming_fields.items():
        converted_value = _convert_update_field_value(tenant_doc, field_name, incoming_value)
        if converted_value is _LEAVE_FIELD_UNTOUCHED:
            # Skip empty settings dicts to prevent mongoengine validation errors
            continue
        setattr(tenant_doc, field_name, converted_value)


def _convert_to_embedded_settings_or_drop_key(tenant_values: dict, field_name: str, settings_class) -> None:
    """Convert a settings dict in place, or drop it entirely so the model default applies."""
    if not tenant_values.get(field_name):
        return
    if any(setting_value for setting_value in tenant_values[field_name].values()):
        tenant_values[field_name] = settings_class(**tenant_values[field_name])
    else:
        tenant_values.pop(field_name)


def _build_tenant_doc_from_payload(tenant_payload: TenantRequest) -> TenantDocument:
    """Build a brand-new ``TenantDocument`` from the request (the owner login is not a tenant field)."""
    tenant_values = tenant_payload.dict(exclude=set(OWNER_LOGIN_FIELDS))
    if tenant_values.get("twilio_accounts"):
        tenant_values["twilio_accounts"] = [TwilioAccount(**account) for account in tenant_values["twilio_accounts"]]
    if tenant_values.get("openai_settings"):
        # Drop the masked-form value so we never persist a fake "x**y" key.
        _drop_redacted_shared_openai_key(tenant_values["openai_settings"])
    _convert_to_embedded_settings_or_drop_key(tenant_values, "openai_settings", OpenAISettings)
    _convert_to_embedded_settings_or_drop_key(tenant_values, "anthropic_settings", AnthropicSettings)
    _convert_to_embedded_settings_or_drop_key(tenant_values, "google_llm_settings", GoogleLLMSettings)
    if tenant_values.get("features"):
        tenant_values["features"] = [FeatureTenant(**feature) for feature in tenant_values["features"]]
    tenant_values["tools"] = _dedupe_and_verify_tool_names(tenant_values.get("tools"))
    return TenantDocument(**tenant_values)


def _fill_missing_creator_and_members(tenant_doc: TenantDocument, current_user: User) -> None:
    """The creator becomes the first member of the tenant."""
    tenant_doc.creator_email = tenant_doc.creator_email or current_user.email
    tenant_doc.access_users = tenant_doc.access_users or [current_user.email]


def _prepare_tenant_doc_from_payload(
    tenant_name: str, tenant_payload: TenantCreateRequest, is_creation: bool
) -> TenantDocument:
    """The tenant document to save (not saved yet): the stored tenant with the payload applied in place, or a new
    one built from the payload; 400 when a creation names an existing tenant."""
    tenant_doc: TenantDocument = TenantDocument.objects(name=tenant_name).first()

    if tenant_doc and is_creation:
        raise HTTPException(status_code=400, detail="Tenant already exists")
    if tenant_doc:
        _apply_payload_to_tenant_doc(tenant_doc, tenant_payload)
    else:
        tenant_doc = _build_tenant_doc_from_payload(tenant_payload)
    return tenant_doc


def _persist_tenant_doc(tenant_doc: TenantDocument) -> None:
    """Save the tenant document (400 with the error text when the save fails) and reload it from the DB."""
    try:
        tenant_doc.save()
    except Exception as error:
        logger.error(error)
        raise HTTPException(status_code=400, detail=str(error))
    tenant_doc.reload()


@tenants_router.post("", response_model=TenantRequest)
@tenants_router.put("/{tenant_name}")
async def create_or_update_tenant(
    tenant_payload: TenantCreateRequest,
    http_request: Request,
    current_user: User = Security(authorize_current_user, scopes=["staff", "grant_access"]),
    tenant_name: Optional[str] = None,
):
    """
    This endpoint allows a staff user to create a new tenant. The tenant name is
    converted to lowercase and spaces are replaced with underscores. The name
    must be alphanumeric and may contain digits, underscores, or hyphens. If a
    tenant with the same name already exists, an error is raised. With
    ``owner_email`` + ``owner_password`` (POST only) the business owner's dashboard
    login is created too (role ``user``, member of this tenant).

    Args:
        tenant_payload (TenantCreateRequest): The request object containing tenant details.
        current_user (User, optional): The user making the request, verified by token.
                                       Defaults to Depends(load_user_from_token).

    Raises:
        HTTPException: If the user is not staff.
        HTTPException: If the tenant name is not alphanumeric or contains invalid characters.
        HTTPException: If a tenant with the same name already exists.

    Returns:
        Tenant: The created tenant object.
    """
    _verify_tenant_creation_request(http_request, tenant_payload, current_user)

    tenant_name = tenant_name or tenant_payload.name
    tenant_doc = _prepare_tenant_doc_from_payload(tenant_name, tenant_payload, http_request.method == "POST")
    result_messages: List[str] = []
    seed_default_prompt(tenant_doc)
    _fill_missing_creator_and_members(tenant_doc, current_user)
    _persist_tenant_doc(tenant_doc)
    refresh_cached_tenant_doc(tenant_name)
    ensure_chat_and_call_settings(tenant_name)
    result_messages.append("Tenant created successfully")
    if http_request.method == "POST" and tenant_payload.owner_email:
        enroll_member_in_tenant(
            tenant_payload.owner_email, tenant_payload.owner_password, tenant_doc.description or tenant_name, tenant_doc
        )
        result_messages.append(f"Owner user {tenant_payload.owner_email} created")
    return JSONResponse(content={"messages": result_messages})


class TenantDetail(BaseModel):
    """Full tenant detail returned to staff (includes the tokens and every account)."""
    name: str
    status: str
    description: str
    creator_email: Optional[str] = None
    gpt_model: Optional[str]
    openai_api_key: Optional[str]
    access_users: List[str]
    twilio_accounts: List[TwilioAccountModel]
    waha_accounts: Optional[List[WahaAccountModel]] = None
    google_llm_settings: Optional[GoogleLLMSettingsModel] = None
    anthropic_settings: Optional[AnthropicSettingsModel] = None
    phone_numbers: List[str]
    manual_mode: Optional[bool] = False
    openai_settings: Optional[OpenAISettingsModel] = None
    features: List[FeatureTenantModel] = []
    default_language: Optional[str] = "he"
    default_timezone: Optional[str] = TENANT_DEFAULT_TIMEZONE
    owner_phone_number: Optional[str] = None
    tools: List[str] = []


def _collect_channel_phone_numbers(tenant_doc: TenantDocument) -> List[str]:
    """Distinct phone numbers of the tenant's Twilio and WAHA accounts."""
    twilio_phone_numbers = {
        account.phone_number for account in tenant_doc.twilio_accounts or [] if account.phone_number
    }
    waha_phone_numbers = {account.phone_number for account in tenant_doc.waha_accounts or [] if account.phone_number}
    return list(twilio_phone_numbers | waha_phone_numbers)


def _build_redacted_openai_settings_model(tenant_doc: TenantDocument) -> Optional[OpenAISettingsModel]:
    """The tenant's OpenAI settings as the response model, or None when it has none; a shared env-var key comes
    back redacted."""
    openai_settings_model = (
        OpenAISettingsModel(**tenant_doc.openai_settings.to_mongo().to_dict()) if tenant_doc.openai_settings else None
    )
    if openai_settings_model:
        openai_settings_model.openai_api_key = redact_openai_api_key_if_shared(openai_settings_model.openai_api_key)
    return openai_settings_model


def _build_shared_detail_fields(tenant_doc: TenantDocument) -> dict:
    """Convert the tenant's embedded documents into the Pydantic models of the response; the shared OpenAI key is
    masked.
    """
    openai_settings_model = _build_redacted_openai_settings_model(tenant_doc)

    return {
        "phone_numbers": _collect_channel_phone_numbers(tenant_doc),
        "openai_settings": openai_settings_model,
        "anthropic_settings": (
            AnthropicSettingsModel(**tenant_doc.anthropic_settings.to_mongo().to_dict())
            if tenant_doc.anthropic_settings
            else None
        ),
        "google_llm_settings": (
            GoogleLLMSettingsModel(**tenant_doc.google_llm_settings.to_mongo().to_dict())
            if tenant_doc.google_llm_settings
            else None
        ),
        "features": [FeatureTenantModel(**feature.to_mongo().to_dict()) for feature in tenant_doc.features],
        "twilio_accounts": [TwilioAccountModel(**account.to_mongo().to_dict()) for account in tenant_doc.twilio_accounts],
        "waha_accounts": (
            [WahaAccountModel(**account.to_mongo().to_dict()) for account in tenant_doc.waha_accounts]
            if tenant_doc.waha_accounts
            else None
        ),
    }


def _build_tenant_detail_for_staff(tenant_doc: TenantDocument, shared_fields: dict) -> TenantDetail:
    """TenantDetail (full) for staff."""
    return TenantDetail(
        name=tenant_doc.name,
        status=tenant_doc.status,
        description=tenant_doc.description,
        creator_email=tenant_doc.creator_email,
        openai_api_key=tenant_doc.openai_api_key,
        gpt_model=tenant_doc.gpt_model,
        access_users=tenant_doc.access_users,
        phone_numbers=shared_fields["phone_numbers"],
        manual_mode=tenant_doc.manual_mode,
        twilio_accounts=shared_fields["twilio_accounts"],
        waha_accounts=shared_fields["waha_accounts"],
        openai_settings=shared_fields["openai_settings"],
        features=shared_fields["features"],
        google_llm_settings=shared_fields["google_llm_settings"],
        anthropic_settings=shared_fields["anthropic_settings"],
        default_language=tenant_doc.default_language or "he",
        default_timezone=tenant_doc.default_timezone or TENANT_DEFAULT_TIMEZONE,
        owner_phone_number=tenant_doc.owner_phone_number or None,
        tools=list(tenant_doc.tools or []),
    )


def _build_tenant_detail_for_member(tenant_doc: TenantDocument, shared_fields: dict) -> TenantCustomer:
    """TenantCustomer (reduced detail) for tenant members."""
    return TenantCustomer(
        name=tenant_doc.name,
        status=tenant_doc.status,
        description=tenant_doc.description,
        creator_email=tenant_doc.creator_email,
        gpt_model=tenant_doc.gpt_model,
        access_users=tenant_doc.access_users,
        phone_numbers=shared_fields["phone_numbers"],
        manual_mode=tenant_doc.manual_mode,
        openai_settings=shared_fields["openai_settings"],
        features=shared_fields["features"],
        twilio_accounts=shared_fields["twilio_accounts"],
        waha_accounts=shared_fields["waha_accounts"],
        google_llm_settings=shared_fields["google_llm_settings"],
        anthropic_settings=shared_fields["anthropic_settings"],
        default_language=tenant_doc.default_language or "he",
        default_timezone=tenant_doc.default_timezone or TENANT_DEFAULT_TIMEZONE,
        owner_phone_number=tenant_doc.owner_phone_number or None,
        tools=list(tenant_doc.tools or []),
    )


@tenants_router.get("/{tenant_name}", response_model=Union[TenantDetail, TenantCustomer])
async def fetch_tenant_detail(tenant_name: str, current_user: UserDocument = Security(authorize_current_user)):
    """
    Endpoint to retrieve a tenant by its name.

    Args:
        tenant_name (str): The name of the tenant to retrieve.
        current_user (User, optional): The user making the request, verified by the token.
            Defaults to Depends(load_user_from_token).

    Returns:
        Union[Tenant, TenantCustomer]: The tenant details if the user is staff or a TenantCustomer object if the user has access.

    Raises:
        HTTPException: If the tenant is not found (404).
        HTTPException: If the user does not have access to the tenant (403).
    """
    tenant_doc = TenantDocument.objects(name=tenant_name).first()
    if not tenant_doc:
        raise HTTPException(status_code=404, detail=TENANT_NOT_FOUND_DETAIL)
    tenant_doc.backfill_feature_defaults()
    shared_fields = _build_shared_detail_fields(tenant_doc)
    if current_user.is_staff:
        return _build_tenant_detail_for_staff(tenant_doc, shared_fields)
    if tenant_name in list(current_user.permissions.keys()):
        return _build_tenant_detail_for_member(tenant_doc, shared_fields)
    raise HTTPException(status_code=403, detail="You do not have access to this tenant")
