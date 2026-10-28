"""Admin API: tenants (businesses): list, create with defaults, detail (masked secrets), update, ownership, delete."""

import logging
import os
import re
from typing import List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from maia.models.platform_defaults import create_default_platform_settings
from maia.services.registry import update_tenant_document
from maia.models.tenants import AnthropicSettings, AnthropicSettingsModel, GoogleLLMSettings, GoogleLLMSettingsModel, OpenAISettings, OpenAISettingsModel, PlatformBotSettings, PlatformBotSettingsModel, WahaAccountModel, TenantCustomer, TenantDocument, TwilioAccount, TwilioAccountModel
from maia.models.users import User, UserDocument
from maia.models.settings import CallSettingsDocument, ChatSettingsDocument
from maia.models.tenant_features import FeatureTenant, FeatureTenantModel
from maia.api.security import (
    authenticate_user_token,
    verify_token_extended,
)
from maia.utils.make_automation import make_automation

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Jerusalem"


def ensure_default_chat_call_settings(tenant_name: str) -> None:
    """Create default ChatSettings and CallSettings documents for the tenant if missing."""
    if not ChatSettingsDocument.objects(tenant_name=tenant_name).first():
        chat_settings = ChatSettingsDocument(tenant_name=tenant_name)
        chat_settings.update_all_features()
        chat_settings.save()
    if not CallSettingsDocument.objects(tenant_name=tenant_name).first():
        call_settings = CallSettingsDocument(tenant_name=tenant_name)
        call_settings.update_all_features()
        call_settings.save()


def get_embedded_doc_as_dict(embedded_doc) -> dict:
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
    result = {}
    # returns the fields of the embedded document as a dictionary in python field names
    for field_name in embedded_doc._fields.keys():
        val = getattr(embedded_doc, field_name, None)
        if val is not None:
            result[field_name] = val
    return result


def deep_merge(base: dict, updates: dict) -> dict:
    """
    Deep merge updates into base, returning a new dict.
    Handles nested dictionaries recursively.

    Args:
        base: The base dictionary to merge into
        updates: The dictionary with updates to apply

    Returns:
        dict: A new merged dictionary
    """
    result = base.copy()
    for key, value in updates.items():
        if value is None:
            # Skip None values to preserve existing settings
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge_embedded_settings(existing_doc, new_values: dict) -> dict:
    """
    Merge new values into existing embedded document settings.

    - Preserves existing fields not present in new_values
    - Updates fields present in new_values (with non-None values)
    - Uses deep merge for nested structures (future-proofing)

    Args:
        existing_doc: The existing EmbeddedDocument instance
        new_values: Dictionary of new values to merge

    Returns:
        dict: Merged settings ready for EmbeddedDocument constructor
    """
    existing_settings = get_embedded_doc_as_dict(existing_doc)
    return deep_merge(existing_settings, new_values)


def _mask_api_key(key: str) -> str:
    """Return first char + '**' + last char (or just the value if shorter than 2)."""
    if not key:
        return key
    if len(key) < 2:
        return f"{key}**"
    return f"{key[0]}**{key[-1]}"


def _shared_openai_api_keys() -> List[str]:
    """Non-empty shared OpenAI API keys from env vars."""
    return [k for k in (os.environ.get("MAIA_OPENAI_API_KEY"),) if k]


def mask_openai_api_key_if_shared(key: Optional[str]) -> Optional[str]:
    """If the key matches a shared env-var key, return its masked form. Otherwise return as-is."""
    if not key:
        return key
    if key in _shared_openai_api_keys():
        return _mask_api_key(key)
    return key


def is_masked_shared_openai_api_key(key: Optional[str]) -> bool:
    """True if the value is the masked form of a shared env-var key (i.e. a fake value sent back by the client)."""
    if not key:
        return False
    return key in {_mask_api_key(k) for k in _shared_openai_api_keys()}


TENANT_NOT_FOUND = "Tenant not found"

tenant_router = APIRouter(prefix="/tenants", tags=["Tenants"])


class TenantRequest(BaseModel):
    """Body of tenant creation / update: status, LLM providers, channels, bot settings per platform, features."""
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
    platform_bot_settings: Optional[List[PlatformBotSettingsModel]] = []
    features: List[FeatureTenantModel] = []
    default_language: Optional[str] = "he"
    default_timezone: Optional[str] = DEFAULT_TIMEZONE
    owner_phone_number: Optional[str] = None

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


@tenant_router.get("", response_model=List[TenantRequest])
async def get_tenants(user: UserDocument = Security(verify_token_extended, scopes=["staff"])):
    """
    Retrieve a list of tenants accessible to the user.

    If the user is a staff member, all tenants are retrieved. Otherwise, only tenants that the
    user has access to.

    Args:
        user (User): The user object, obtained through dependency injection
                     and token verification.

    Returns:
        List[Dict[str, str]]: A list of dictionaries, each containing the 'name'
                              and 'description' of a tenant.
    """
    if user.is_staff:
        tenants = TenantDocument.objects().only("name", "description", "status")
    else:
        tenant_names = list(user.permissions.keys())
        tenants = TenantDocument.objects(name__in=tenant_names).only("name", "description", "status")

    return [{"name": tenant.name, "description": tenant.description, "status": tenant.status} for tenant in tenants]


_SKIP_FIELD = object()


def _validate_create_tenant_request(request: Request, tenant_request: TenantRequest, user: User) -> None:
    """Guard the POST (creation) flow: authorisation and tenant-name format."""
    if request.method != "POST":
        return
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="You are not authorized to create a tenant")
    if not tenant_request.name:
        raise HTTPException(status_code=400, detail="Tenant name is required")
    tenant_request.name = tenant_request.name.lower().replace(" ", "_")
    if not re.match("^[a-zA-Z0-9_-]+$", tenant_request.name):
        raise HTTPException(status_code=400, detail="The name must be alphanumeric and may contain digits, _, or -.")


def _merged_embedded_settings(existing, value, settings_cls):
    """Merge an embedded settings payload with what is stored, or skip an empty one."""
    if not value or (isinstance(value, dict) and not any(v for v in value.values())):
        return _SKIP_FIELD
    if not isinstance(value, dict):
        return value
    return settings_cls(**merge_embedded_settings(existing, value))


def _converted_update_value(tenant_doc: TenantDocument, key: str, value):
    """Convert one incoming field into its embedded-document form.

    Returns ``_SKIP_FIELD`` when the field must be left untouched on the document.
    """
    if key == "openai_settings":
        if isinstance(value, dict) and is_masked_shared_openai_api_key(value.get("openai_api_key")):
            # The client sent back the masked form of a shared env-var key — drop it so the
            # existing value in DB is preserved (avoids saving a fake masked key).
            value.pop("openai_api_key", None)
        return _merged_embedded_settings(tenant_doc.openai_settings, value, OpenAISettings)
    if key == "anthropic_settings":
        return _merged_embedded_settings(tenant_doc.anthropic_settings, value, AnthropicSettings)
    if key == "google_llm_settings":
        return _merged_embedded_settings(tenant_doc.google_llm_settings, value, GoogleLLMSettings)
    if not value:
        return value
    if key == "twilio_accounts":
        return [TwilioAccount(**account) for account in value]
    if key == "features":
        return [FeatureTenant(**feature) for feature in value]
    if key == "platform_bot_settings":
        return [PlatformBotSettings(**setting) for setting in value]
    return value


def _apply_tenant_update(tenant_doc: TenantDocument, tenant_request: TenantRequest) -> None:
    """Apply a partial update on an existing tenant."""
    update_fields = tenant_request.dict(exclude_unset=True)
    for key, value in update_fields.items():
        converted = _converted_update_value(tenant_doc, key, value)
        if converted is _SKIP_FIELD:
            # Skip empty settings dicts to prevent mongoengine validation errors
            continue
        setattr(tenant_doc, key, converted)


def _embedded_or_dropped(tenant_dict: dict, key: str, settings_cls) -> None:
    """Convert a settings dict in place, or drop it entirely so the model default applies."""
    if not tenant_dict.get(key):
        return
    if any(v for v in tenant_dict[key].values()):
        tenant_dict[key] = settings_cls(**tenant_dict[key])
    else:
        tenant_dict.pop(key)


def _build_new_tenant_document(tenant_request: TenantRequest) -> TenantDocument:
    """Build a brand-new ``TenantDocument`` from the request."""
    tenant_dict = tenant_request.dict()
    if tenant_dict.get("twilio_accounts"):
        tenant_dict["twilio_accounts"] = [TwilioAccount(**account) for account in tenant_dict["twilio_accounts"]]
    if tenant_dict.get("openai_settings"):
        # Drop the masked-form value so we never persist a fake "x**y" key.
        if is_masked_shared_openai_api_key(tenant_dict["openai_settings"].get("openai_api_key")):
            tenant_dict["openai_settings"].pop("openai_api_key", None)
    _embedded_or_dropped(tenant_dict, "openai_settings", OpenAISettings)
    _embedded_or_dropped(tenant_dict, "anthropic_settings", AnthropicSettings)
    _embedded_or_dropped(tenant_dict, "google_llm_settings", GoogleLLMSettings)
    if tenant_dict.get("platform_bot_settings"):
        tenant_dict["platform_bot_settings"] = [
            PlatformBotSettings(**setting) for setting in tenant_dict["platform_bot_settings"]
        ]
    if tenant_dict.get("features"):
        tenant_dict["features"] = [FeatureTenant(**feature) for feature in tenant_dict["features"]]
    return TenantDocument(**tenant_dict)


def _assign_tenant_ownership(tenant_doc: TenantDocument, user: User) -> None:
    """The creator becomes the first member of the tenant."""
    tenant_doc.creator_email = tenant_doc.creator_email or user.email
    tenant_doc.access_users = tenant_doc.access_users or [user.email]


@tenant_router.post("", response_model=TenantRequest)
@tenant_router.put("/{tenant_name}")
async def create_tenant(
    tenant_request: TenantRequest,
    request: Request,
    user: User = Security(verify_token_extended, scopes=["staff", "grant_access"]),
    tenant_name: Optional[str] = None,
):
    """
    This endpoint allows a staff user to create a new tenant. The tenant name is
    converted to lowercase and spaces are replaced with underscores. The name
    must be alphanumeric and may contain digits, underscores, or hyphens. If a
    tenant with the same name already exists, an error is raised.

    Args:
        tenant_request (TenantRequest): The request object containing tenant details.
        user (User, optional): The user making the request, verified by token.
                               Defaults to Depends(verify_token).

    Raises:
        HTTPException: If the user is not staff.
        HTTPException: If the tenant name is not alphanumeric or contains invalid characters.
        HTTPException: If a tenant with the same name already exists.

    Returns:
        Tenant: The created tenant object.
    """
    _validate_create_tenant_request(request, tenant_request, user)

    tenant_name = tenant_name or tenant_request.name
    tenant_doc: TenantDocument = TenantDocument.objects(name=tenant_name).first()

    if tenant_doc and request.method == "POST":
        raise HTTPException(status_code=400, detail="Tenant already exists")
    if tenant_doc:
        _apply_tenant_update(tenant_doc, tenant_request)
    else:
        tenant_doc = _build_new_tenant_document(tenant_request)
    messages: List[str] = []
    make_automation(tenant_doc)
    if not CallSettingsDocument.objects(tenant_name=tenant_name).first():
        CallSettingsDocument(tenant_name=tenant_name).save()
    if not ChatSettingsDocument.objects(tenant_name=tenant_name).first():
        ChatSettingsDocument(tenant_name=tenant_name).save()
    _assign_tenant_ownership(tenant_doc, user)
    try:
        tenant_doc.save()
    except Exception as e:
        logger.error(e)
        raise HTTPException(status_code=400, detail=str(e))
    tenant_doc.reload()
    update_tenant_document(tenant_name)
    ensure_default_chat_call_settings(tenant_name)
    messages.append("Tenant created successfully")
    return JSONResponse(content={"messages": messages})


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
    platform_bot_settings: List[PlatformBotSettingsModel] = []
    features: List[FeatureTenantModel] = []
    default_language: Optional[str] = "he"
    default_timezone: Optional[str] = DEFAULT_TIMEZONE
    owner_phone_number: Optional[str] = None


def _collect_tenant_phone_numbers(tenant: TenantDocument) -> List[str]:
    """Distinct phone numbers of the tenant's Twilio and WAHA accounts."""
    twilio_numbers = {account.phone_number for account in tenant.twilio_accounts or [] if account.phone_number}
    waha_numbers = {account.phone_number for account in tenant.waha_accounts or [] if account.phone_number}
    return list(twilio_numbers | waha_numbers)


def _build_tenant_response_context(tenant: TenantDocument) -> dict:
    """Convert the tenant's embedded documents into the Pydantic models of the response; the shared OpenAI key is
    masked.
    """
    openai_settings = (
        OpenAISettingsModel(**tenant.openai_settings.to_mongo().to_dict()) if tenant.openai_settings else None
    )
    if openai_settings:
        openai_settings.openai_api_key = mask_openai_api_key_if_shared(openai_settings.openai_api_key)

    return {
        "phone_numbers": _collect_tenant_phone_numbers(tenant),
        "openai_settings": openai_settings,
        "anthropic_settings": (
            AnthropicSettingsModel(**tenant.anthropic_settings.to_mongo().to_dict())
            if tenant.anthropic_settings
            else None
        ),
        "google_llm_settings": (
            GoogleLLMSettingsModel(**tenant.google_llm_settings.to_mongo().to_dict())
            if tenant.google_llm_settings
            else None
        ),
        "platform_bot_settings": [
            PlatformBotSettingsModel(**settings.to_mongo().to_dict())
            for settings in (tenant.platform_bot_settings or create_default_platform_settings())
        ],
        "features": [FeatureTenantModel(**feature.to_mongo().to_dict()) for feature in tenant.features],
        "twilio_accounts": [TwilioAccountModel(**account.to_mongo().to_dict()) for account in tenant.twilio_accounts],
        "waha_accounts": (
            [WahaAccountModel(**account.to_mongo().to_dict()) for account in tenant.waha_accounts]
            if tenant.waha_accounts
            else None
        ),
    }


def _build_staff_tenant_detail(tenant: TenantDocument, context: dict) -> TenantDetail:
    """TenantDetail (full) for staff."""
    return TenantDetail(
        name=tenant.name,
        status=tenant.status,
        description=tenant.description,
        creator_email=tenant.creator_email,
        openai_api_key=tenant.openai_api_key,
        gpt_model=tenant.gpt_model,
        access_users=tenant.access_users,
        phone_numbers=context["phone_numbers"],
        manual_mode=tenant.manual_mode,
        twilio_accounts=context["twilio_accounts"],
        waha_accounts=context["waha_accounts"],
        openai_settings=context["openai_settings"],
        features=context["features"],
        platform_bot_settings=context["platform_bot_settings"],
        google_llm_settings=context["google_llm_settings"],
        anthropic_settings=context["anthropic_settings"],
        default_language=tenant.default_language or "he",
        default_timezone=tenant.default_timezone or DEFAULT_TIMEZONE,
        owner_phone_number=tenant.owner_phone_number or None,
    )


def _build_customer_tenant_detail(tenant: TenantDocument, context: dict) -> TenantCustomer:
    """TenantCustomer (reduced detail) for tenant members."""
    return TenantCustomer(
        name=tenant.name,
        status=tenant.status,
        description=tenant.description,
        creator_email=tenant.creator_email,
        gpt_model=tenant.gpt_model,
        access_users=tenant.access_users,
        phone_numbers=context["phone_numbers"],
        manual_mode=tenant.manual_mode,
        openai_settings=context["openai_settings"],
        features=context["features"],
        twilio_accounts=context["twilio_accounts"],
        waha_accounts=context["waha_accounts"],
        platform_bot_settings=context["platform_bot_settings"],
        google_llm_settings=context["google_llm_settings"],
        anthropic_settings=context["anthropic_settings"],
        default_language=tenant.default_language or "he",
        default_timezone=tenant.default_timezone or DEFAULT_TIMEZONE,
        owner_phone_number=tenant.owner_phone_number or None,
    )


@tenant_router.get("/{tenant_name}", response_model=Union[TenantDetail, TenantCustomer])
async def get_tenant(tenant_name: str, user: UserDocument = Security(verify_token_extended)):
    """
    Endpoint to retrieve a tenant by its name.

    Args:
        tenant_name (str): The name of the tenant to retrieve.
        user (User, optional): The user making the request, verified by the token. Defaults to Depends(verify_token).

    Returns:
        Union[Tenant, TenantCustomer]: The tenant details if the user is staff or a TenantCustomer object if the user has access.

    Raises:
        HTTPException: If the tenant is not found (404).
        HTTPException: If the user does not have access to the tenant (403).
    """
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail=TENANT_NOT_FOUND)
    tenant.update_all_features()
    context = _build_tenant_response_context(tenant)
    if user.is_staff:
        return _build_staff_tenant_detail(tenant, context)
    if tenant_name in list(user.permissions.keys()):
        return _build_customer_tenant_detail(tenant, context)
    raise HTTPException(status_code=403, detail="You do not have access to this tenant")


@tenant_router.delete("/{tenant_name}")
async def delete_tenant(tenant_name: str, user: User = Depends(authenticate_user_token)):
    """
    Deletes a tenant.
    """
    tenant = TenantDocument.objects(name=tenant_name).first()
    if not tenant:
        raise HTTPException(status_code=404, detail=TENANT_NOT_FOUND)
    if user.is_staff or user.email == tenant.creator_email:
        tenant.delete()
        return JSONResponse(content={"message": "Tenant deleted successfully"})
    else:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this tenant")
