"""The tenant (business) document and its embedded settings: channels, LLM providers, features."""

import logging
from datetime import datetime
from typing import List, Literal, Optional

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmailField,
    EmbeddedDocument,
    EmbeddedDocumentField,
    FloatField,
    ListField,
    StringField,
)
from pydantic import BaseModel

from maia.models.tenant_features import FeatureTenant, FeatureTenantModel, DEFAULT_TENANT_FEATURES

logger = logging.getLogger(__name__)


class TwilioAccountModel(BaseModel):
    """Twilio Voice credentials (API view)."""
    account_sid: str
    auth_token: str
    phone_number: str


class WahaAccountModel(BaseModel):
    """A WAHA (WhatsApp) account (API view): number, webhook, connection status."""
    phone_number: str
    webhook_url: Optional[str] = None
    is_connected: bool = False
    session_status: Optional[Literal["STOPPED", "STARTING", "SCAN_QR_CODE", "WORKING", "FAILED"]] = "STOPPED"
    last_status_update: Optional[datetime] = None


class OpenAISettingsModel(BaseModel):
    """OpenAI key and model of the tenant (API view)."""
    openai_api_key: Optional[str] = None
    gpt_model: Optional[str] = None
    temperature: float = 0.5


class AnthropicSettingsModel(BaseModel):
    """Anthropic key and model of the tenant (API view)."""
    anthropic_api_key: Optional[str] = None
    model: Optional[str] = None


class GoogleLLMSettingsModel(BaseModel):
    """Google LLM key and model of the tenant (API view)."""
    google_api_key: Optional[str] = None
    model: Optional[str] = None


class TwilioAccount(EmbeddedDocument):
    """Twilio Voice credentials of a tenant (used to fetch call recordings). WhatsApp goes through WAHA."""

    account_sid = StringField(required=True)
    auth_token = StringField(required=True)
    phone_number = StringField(required=False, null=True)


class WahaAccount(EmbeddedDocument):
    """A WAHA (WhatsApp) account of the tenant: the connected number, its webhook and session status."""
    phone_number = StringField(required=True)
    webhook_url = StringField(required=False)
    is_connected = BooleanField(default=False)
    session_status = StringField(required=False, default="STOPPED")
    last_status_update = DateTimeField(required=False)


class OpenAISettings(EmbeddedDocument):
    """OpenAI key and model of the tenant (empty key = the platform's shared key)."""
    openai_api_key = StringField(required=False, default="")
    gpt_model = StringField(required=False, default="gpt-4o-mini")
    temperature = FloatField(default=0.5)


class AnthropicSettings(EmbeddedDocument):
    """Anthropic key and model of the tenant."""
    anthropic_api_key = StringField(required=False, default="")
    model = StringField(required=False, default="claude-sonnet")


class GoogleLLMSettings(EmbeddedDocument):
    """Google LLM key and model of the tenant."""
    google_api_key = StringField(required=False, default="")
    model = StringField(required=False, default="google-model")


def _fill_missing_meta_data(feature: FeatureTenant, default_meta_data: dict) -> bool:
    """Copy into the feature's meta_data every default key it lacks; returns True when at least one key was added."""
    has_added_key = False
    for meta_key, default_value in default_meta_data.items():
        if meta_key not in feature.meta_data:
            feature.meta_data[meta_key] = default_value
            has_added_key = True
    return has_added_key


class TenantDocument(Document):
    """A business using Maia: channels (Twilio Voice, WAHA), LLM provider settings, features, language, timezone,
    the connected Google Sheet and the tools the bot may call. Strict: a stored document may not carry unknown
    fields.
    """
    name = StringField(required=True, unique=True)
    status = StringField(default="active")
    description = StringField(default="")
    openai_api_key = StringField()
    gpt_model = StringField()
    creator_email = EmailField(required=False)
    access_users = ListField(StringField())
    manual_mode = BooleanField(default=False)
    # Twilio Voice credentials (call recordings); WhatsApp accounts are `waha_accounts`
    twilio_accounts = ListField(EmbeddedDocumentField(TwilioAccount))
    waha_accounts = ListField(EmbeddedDocumentField(WahaAccount))
    openai_settings = EmbeddedDocumentField(OpenAISettings, required=False, default=OpenAISettings())
    anthropic_settings = EmbeddedDocumentField(AnthropicSettings, required=False, default=AnthropicSettings())
    google_llm_settings = EmbeddedDocumentField(GoogleLLMSettings, required=False, default=GoogleLLMSettings())
    features = ListField(EmbeddedDocumentField(FeatureTenant), default=DEFAULT_TENANT_FEATURES)
    default_language = StringField(required=False, default="he")  # Default language for the tenant (he/en)
    default_timezone = StringField(required=False, default="Asia/Jerusalem")  # Default timezone
    owner_phone_number = StringField(required=False, default="")  # Owner phone number for notifications
    google_sheet_id = StringField(required=False, default="")  # Spreadsheet the bot may read (st_find_sheet_rows)
    # Names of the `@llm_tool` functions bound to the bot on every turn (chosen on the dashboard; applied by
    # `tenants/hooks.py`). Empty: the bot answers from the knowledge base only.
    tools = ListField(StringField(), default=[])

    meta = {
        "collection": "tenant",
        "indexes": [
            {"fields": ["creator_email"]},
        ],
    }

    def find_waha_account_by_phone_number(self, tenant_phone_number: str) -> Optional[WahaAccount]:
        """Get WAHA account by phone number (primary identifier)"""
        if not tenant_phone_number:
            return None
        for waha_account in self.waha_accounts:
            if waha_account.phone_number and (
                waha_account.phone_number.replace("+", "") == tenant_phone_number.replace("+", "")
            ):
                return waha_account
        return None

    def _append_default_feature(self, feature_name: str, default_definition: dict) -> FeatureTenant:
        """Append a FeatureTenant built from the default definition to the tenant's features (no save); returns it."""
        added_feature = FeatureTenant(
            name=feature_name, enabled=default_definition["enabled"], meta_data=default_definition["meta_data"]
        )
        self.features.append(added_feature)
        return added_feature

    def ensure_feature(self, feature_name: str) -> FeatureTenantModel:
        """
        Returns the feature with the given name if it exists in the tenant's features.
        """
        feature = next((candidate for candidate in self.features if candidate.name == feature_name), None)
        if not feature:
            default_definition = self.resolve_feature_defaults(feature_name)
            feature = self._append_default_feature(feature_name, default_definition)
            self.save()
        return feature

    @classmethod
    def resolve_feature_defaults(cls, feature_name: str) -> dict:
        """Default definition of a tenant feature: default_platform (waha) and call_auto_reset (both phases on);
        unknown names are disabled.
        """
        if feature_name == "default_platform":
            return {"enabled": True, "meta_data": {"platform": "waha"}}
        elif feature_name == "call_auto_reset":
            # Controls the automatic conversation reset around phone/SIP calls. `on_start` resets the
            # telephone conversation before placing/answering a call; `on_end` resets it once the call
            # completes. Both phases are enabled by default. The reset
            # is platform-scoped (see end_customer_conversation) and platforms are always kept in separate
            # conversations, so it only clears the telephone thread, never the WhatsApp/other-platform
            # conversation.
            return {"enabled": True, "meta_data": {"on_start": True, "on_end": True}}
        else:
            return {"enabled": False, "meta_data": {}}

    def backfill_feature_defaults(self):
        """Backfill the known tenant features and their missing meta_data keys; returns True when something was saved.
        """
        has_changes = False
        known_feature_names = ["default_platform", "call_auto_reset"]

        stored_features_by_name = {feature.name: feature for feature in self.features}

        for feature_name in known_feature_names:
            default_definition = self.resolve_feature_defaults(feature_name)

            if feature_name in stored_features_by_name:
                stored_feature = stored_features_by_name[feature_name]
                if _fill_missing_meta_data(stored_feature, default_definition["meta_data"]):
                    has_changes = True
            else:
                self._append_default_feature(feature_name, default_definition)
                has_changes = True

        if has_changes:
            try:
                self.save()
                return True
            except Exception as error:
                logger.error(f"Error saving {self.name}: {str(error)}")
                return False
        else:
            return False


class TenantCustomer(BaseModel):
    """Reduced tenant detail returned to tenant members (API view)."""
    name: str
    status: str
    description: str
    creator_email: str
    access_users: List[str]
    manual_mode: bool
    gpt_model: Optional[str] = None
    phone_numbers: List[str] = []
    openai_settings: Optional[OpenAISettingsModel] = None
    twilio_accounts: Optional[List[TwilioAccountModel]] = None
    waha_accounts: Optional[List[WahaAccountModel]] = None
    google_llm_settings: Optional[GoogleLLMSettingsModel] = None
    anthropic_settings: Optional[AnthropicSettingsModel] = None
    features: List[FeatureTenantModel] = []
    default_language: str = "he"
    default_timezone: str = "Asia/Jerusalem"
    owner_phone_number: Optional[str] = None
    tools: List[str] = []


def should_auto_reset_call_conversation(tenant_name: str, call_phase: str) -> bool:
    """True when the tenant should auto-reset the call conversation at the given phase.

    `call_phase` is "on_start" (before placing/answering a call) or "on_end" (after a call completes).
    Reads the `call_auto_reset` feature directly (no side effects, safe on hot call paths) and
    defaults to enabled for both phases so tenants without the feature keep today's behaviour.
    """
    tenant_doc = TenantDocument.objects(name=tenant_name).only("features").first()
    if not tenant_doc:
        return True
    auto_reset_feature = next(
        (feature for feature in (tenant_doc.features or []) if feature.name == "call_auto_reset"), None
    )
    if auto_reset_feature is None:
        return True
    if not auto_reset_feature.enabled:
        return False
    return bool((auto_reset_feature.meta_data or {}).get(call_phase, True))
