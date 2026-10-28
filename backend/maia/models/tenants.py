"""The tenant (business) document and its embedded settings: channels, LLM providers, bot settings per platform, features."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import pytz
from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
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
    """A WAHA (WhatsApp) account (API view): number, webhook, worker, connection status."""
    phone_number: str
    webhook_url: Optional[str] = None
    server_url: Optional[str] = None  # WAHA worker base URL; None = the default server
    is_connected: bool = False
    session_status: Optional[Literal["STOPPED", "STARTING", "SCAN_QR_CODE", "WORKING", "FAILED"]] = "STOPPED"
    status_history: Optional[List[Dict[str, Any]]] = []
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


class WorkingHoursModel(BaseModel):
    """Bot hours of one weekday (API view)."""
    day: str
    start_time: str  # Format: "HH:MM" (24h format)
    end_time: str  # Format: "HH:MM" (24h format)
    enabled: bool = True
    id: Optional[str] = None  # Optional ID for frontend tracking


class PlatformBotSettingsModel(BaseModel):
    """Bot mode of one platform (API view): always_bot, always_manual, or scheduled with working hours."""
    platform: str  # "waha", "web", "email", "telephone"
    mode: str = "always_bot"  # "always_bot", "always_manual", "scheduled"
    working_hours: List[WorkingHoursModel] = []
    timezone: str = "UTC"


class TwilioAccount(EmbeddedDocument):
    """Twilio Voice credentials of a tenant (used to fetch call recordings). WhatsApp goes through WAHA."""

    account_sid = StringField(required=True)
    auth_token = StringField(required=True)
    phone_number = StringField(required=False, null=True)


class WahaAccount(EmbeddedDocument):
    """A WAHA (WhatsApp) account of the tenant: the connected number, its webhook, worker and session status."""
    phone_number = StringField(required=True)
    webhook_url = StringField(required=False)
    server_url = StringField(required=False)  # WAHA worker base URL; empty = the default server
    is_connected = BooleanField(default=False)
    session_status = StringField(required=False, default="STOPPED")
    last_status_update = DateTimeField(required=False)
    status_history = ListField(DictField(), default=list)


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


class WorkingHours(EmbeddedDocument):
    """Bot hours of one weekday (HH:MM start / end, enabled)."""
    id = StringField()  # Optional ID for frontend tracking
    day = StringField(
        required=True, choices=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    )
    start_time = StringField(required=True)  # Format: "HH:MM" (24h format)
    end_time = StringField(required=True)  # Format: "HH:MM" (24h format)
    enabled = BooleanField(default=True)


class PlatformBotSettings(EmbeddedDocument):
    """Bot mode of one platform: always_bot, always_manual, or scheduled with working hours in a timezone."""
    platform = StringField(
        required=True,
        choices=["web", "email", "telephone", "waha"],
    )
    mode = StringField(required=True, choices=["always_bot", "always_manual", "scheduled"], default="always_bot")
    working_hours = ListField(EmbeddedDocumentField(WorkingHours), default=[])
    timezone = StringField(default="UTC")  # Timezone for the working hours


class TenantDocument(Document):
    """A business using Maia: channels (Twilio Voice, WAHA), LLM provider settings, bot mode per platform, features,
    language, timezone and the connected Google Sheet. Strict: a stored document may not carry unknown fields.
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
    platform_bot_settings = ListField(EmbeddedDocumentField(PlatformBotSettings), default=[])
    features = ListField(EmbeddedDocumentField(FeatureTenant), default=tenant_default_features)
    default_language = StringField(required=False, default="he")  # Default language for the tenant (he/en)
    default_timezone = StringField(required=False, default="Asia/Jerusalem")  # Default timezone
    owner_phone_number = StringField(required=False, default="")  # Owner phone number for notifications
    google_sheet_id = StringField(required=False, default="")  # Spreadsheet the bot may read (st_search_sheet)

    meta = {
        "collection": "tenant",
        "indexes": [
            {"fields": ["creator_email"]},
        ],
    }

    def get_twilio_by_phone_number(self, phone_number: str) -> Optional[TwilioAccount]:
        """Twilio Voice account whose number matches ``phone_number`` (digits compared without '+')."""
        for account in self.twilio_accounts:
            if (account.phone_number or "").replace("+", "") == phone_number.replace("+", ""):
                return account
        return None

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

    def get_platform_bot_settings(self, platform: str) -> PlatformBotSettings:
        """
        Returns the bot settings for the given platform.
        If no settings exist for the platform, creates default settings.
        """
        platform_setting = next((p for p in self.platform_bot_settings if p.platform == platform), None)
        if not platform_setting:
            # Create default settings for the platform
            platform_setting = PlatformBotSettings(
                platform=platform, mode="always_bot", working_hours=[], timezone="Asia/Jerusalem"
            )
            self.platform_bot_settings.append(platform_setting)
            self.save()
        return platform_setting

    def is_bot_active_for_platform(self, platform: str, current_time=None) -> bool:
        """
        Determines if the bot should be active for the given platform at the current time.
        Returns True if bot should respond, False if it should be in manual mode.
        """
        # Check global manual mode first
        if self.manual_mode:
            return False

        platform_settings = self.get_platform_bot_settings(platform)

        if platform_settings.mode == "always_bot":
            return True
        elif platform_settings.mode == "always_manual":
            return False
        elif platform_settings.mode == "scheduled":
            if current_time is None:
                current_time = datetime.now(tz=timezone.utc)

            # Convert to platform timezone
            try:
                tz = pytz.timezone(platform_settings.timezone)
                local_time = current_time.astimezone(tz)
            except Exception:
                # Fallback to UTC if timezone is invalid
                local_time = current_time

            # Get current day and time
            current_day = local_time.strftime("%A").lower()
            current_time_obj = local_time.time()

            # Check if current time falls within any working hours for the current day
            for working_hour in platform_settings.working_hours:
                if working_hour.day == current_day and working_hour.enabled:
                    try:
                        # Parse time strings to time objects for proper comparison
                        start_time = datetime.strptime(working_hour.start_time, "%H:%M").time()
                        end_time = datetime.strptime(working_hour.end_time, "%H:%M").time()

                        # Handle overnight shifts (e.g., 22:00 to 06:00)
                        if start_time <= end_time:
                            # Normal day shift
                            if start_time <= current_time_obj <= end_time:
                                return True
                        else:
                            # Overnight shift
                            if current_time_obj >= start_time or current_time_obj <= end_time:
                                return True
                    except ValueError:
                        # If time parsing fails, fallback to string comparison
                        current_time_str = local_time.strftime("%H:%M")
                        if working_hour.start_time <= current_time_str <= working_hour.end_time:
                            return True

            # If no working hours match for the current time, return False
            return False

        # Default to bot active if mode is unknown
        return True


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
    platform_bot_settings: List[PlatformBotSettingsModel] = []
    google_llm_settings: Optional[GoogleLLMSettingsModel] = None
    anthropic_settings: Optional[AnthropicSettingsModel] = None
    features: List[FeatureTenantModel] = []
    default_language: str = "he"
    default_timezone: str = "Asia/Jerusalem"
    owner_phone_number: Optional[str] = None


def tenant_separates_platforms(tenant_name: str) -> bool:
    """Always True: every platform (WhatsApp, telephone, ...) gets its own conversation for a number.

    Kept as a function so callers stay unchanged; `tenant_name` is ignored.
    """
    return True


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
