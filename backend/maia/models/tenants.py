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

from maia.models.tenant_features import FeatureTenant, FeatureTenantModel, tenant_default_features

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
    features = ListField(EmbeddedDocumentField(FeatureTenant), default=tenant_default_features)
    default_language = StringField(required=False, default="he")  # Default language for the tenant (he/en)
    default_timezone = StringField(required=False, default="Asia/Jerusalem")  # Default timezone
    owner_phone_number = StringField(required=False, default="")  # Owner phone number for notifications
    google_sheet_id = StringField(required=False, default="")  # Spreadsheet the bot may read (st_search_sheet)
    # Names of the `@gpt_tool` functions bound to the bot on every turn (chosen on the dashboard; applied by
    # `tenants/hooks.py`). Empty: the bot answers from the knowledge base only.
    tools = ListField(StringField(), default=[])

    meta = {
        "collection": "tenant",
        "indexes": [
            {"fields": ["creator_email"]},
        ],
    }

    def get_waha_by_phone_number(self, phone_number: str) -> Optional[WahaAccount]:
        """Get WAHA account by phone number (primary identifier)"""
        if not phone_number:
            return None
        for account in self.waha_accounts:
            if account.phone_number and account.phone_number.replace("+", "") == phone_number.replace("+", ""):
                return account
        return None

    def get_feature_by_name(self, feature_name: str) -> FeatureTenantModel:
        """
        Returns the feature with the given name if it exists in the tenant's features.
        """
        feature = next((f for f in self.features if f.name == feature_name), None)
        if not feature:
            default_config = self.get_default_feature_config(feature_name)
            feature = FeatureTenant(
                name=feature_name, enabled=default_config["enabled"], meta_data=default_config["meta_data"]
            )
            self.features.append(feature)
            self.save()
        return feature

    @classmethod
    def get_default_feature_config(cls, feature_name: str) -> dict:
        """Default definition of a tenant feature: default_platform (waha) and call_auto_reset (both phases on);
        unknown names are disabled.
        """
        if feature_name == "default_platform":
            return {"enabled": True, "meta_data": {"platform": "waha"}}
        elif feature_name == "call_auto_reset":
            # Controls the automatic conversation reset around phone/SIP calls. `on_start` resets the
            # telephone conversation before placing/answering a call; `on_end` resets it once the call
            # completes. Both phases are enabled by default. The reset
            # is platform-scoped (see reset_conversation) and platforms are always kept in separate
            # conversations, so it only clears the telephone thread, never the WhatsApp/other-platform
            # conversation.
            return {"enabled": True, "meta_data": {"on_start": True, "on_end": True}}
        else:
            return {"enabled": False, "meta_data": {}}

    def update_all_features(self):
        """Backfill the known tenant features and their missing meta_data keys; returns True when something was saved.
        """
        updated = False
        all_default_features = ["default_platform", "call_auto_reset"]

        existing_features = {feature.name: feature for feature in self.features}

        for feature_name in all_default_features:
            default_config = self.get_default_feature_config(feature_name)

            if feature_name in existing_features:
                existing_feature = existing_features[feature_name]
                feature_updated = False

                for key, value in default_config["meta_data"].items():
                    if key not in existing_feature.meta_data:
                        existing_feature.meta_data[key] = value
                        feature_updated = True

                if feature_updated:
                    updated = True
            else:
                new_feature = FeatureTenant(
                    name=feature_name, enabled=default_config["enabled"], meta_data=default_config["meta_data"]
                )
                self.features.append(new_feature)
                updated = True

        if updated:
            try:
                self.save()
                return True
            except Exception as e:
                logger.error(f"Error saving {self.name}: {str(e)}")
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


def call_auto_reset_enabled(tenant_name: str, phase: str) -> bool:
    """True when the tenant should auto-reset the call conversation at the given phase.

    `phase` is "on_start" (before placing/answering a call) or "on_end" (after a call completes).
    Reads the `call_auto_reset` feature directly (no side effects, safe on hot call paths) and
    defaults to enabled for both phases so tenants without the feature keep today's behaviour.
    """
    tenant = TenantDocument.objects(name=tenant_name).only("features").first()
    if not tenant:
        return True
    feat = next((f for f in (tenant.features or []) if f.name == "call_auto_reset"), None)
    if feat is None:
        return True
    if not feat.enabled:
        return False
    return bool((feat.meta_data or {}).get(phase, True))
