"""Chat settings (delay, features) and call settings (voice, speed, Realtime tuning) of a tenant."""

import logging
from typing import Dict, List, Literal, Optional

from mongoengine import (
    BooleanField,
    DictField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    FloatField,
    IntField,
    ListField,
    StringField,
)
from odmantic import EmbeddedModel, Field, Model
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class FeatureSettingsModel(Model):
    """A feature flag of the chat settings as the API sees it: enabled, meta_data (its parameters), options and
    help texts.
    """
    name: str
    enabled: bool = False
    meta_data: Dict = Field(default={})
    options: Dict = Field(default={})
    description: str = ""
    he_description: str = ""
    # Optional per-field help text shown in the UI, keyed by meta_data key.
    # Shape: { "<field_key>": {"en": "...", "he": "..."} }
    meta_help: Dict = Field(default={})


class ChatSettings(BaseModel):
    """Chat behaviour of a tenant: the human-like delay that batches a burst of messages, and feature flags."""

    tenant_name: str
    chatbot_human_like_delay: bool = False
    chatbot_human_like_time: int = 0
    features: Optional[List[FeatureSettingsModel]] = []


class FeatureSettings(EmbeddedDocument):
    """A feature flag stored inside the chat settings."""
    name = StringField(required=True)
    enabled = BooleanField(default=False)
    meta_data = DictField(default={})
    options = DictField(default=dict)
    description = StringField(default="")
    he_description = StringField(default="")
    # Optional per-field help text. Same shape as FeatureSettingsModel.meta_help.
    meta_help = DictField(default=dict)


def _build_feature_flag_from_defaults(feature_flag_name: str, feature_defaults: dict) -> FeatureSettings:
    """A new FeatureSettings built from the feature's default definition (not appended, not saved)."""
    return FeatureSettings(
        name=feature_flag_name,
        enabled=feature_defaults["enabled"],
        meta_data=feature_defaults["meta_data"],
        options=feature_defaults["options"],
        description=feature_defaults["description"],
        he_description=feature_defaults["he_description"],
        meta_help=feature_defaults.get("meta_help", {}),
    )


def _align_feature_flag_with_defaults(stored_flag: FeatureSettings, feature_defaults: dict) -> bool:
    """Sync a stored feature flag with its defaults (options, empty descriptions); returns True when it changed."""
    is_flag_changed = False
    if not hasattr(stored_flag, "options") or stored_flag.options is None:
        stored_flag.options = {}
        is_flag_changed = True

    # Update options: always sync with defaults (override if different)
    for option_name, default_value in feature_defaults["options"].items():
        if option_name not in stored_flag.options or stored_flag.options[option_name] != default_value:
            stored_flag.options[option_name] = default_value
            is_flag_changed = True

    if not hasattr(stored_flag, "description") or not stored_flag.description:
        stored_flag.description = feature_defaults["description"]
        is_flag_changed = True

    if not hasattr(stored_flag, "he_description") or not stored_flag.he_description:
        stored_flag.he_description = feature_defaults["he_description"]
        is_flag_changed = True

    return is_flag_changed


class ChatSettingsDocument(Document):
    """Chat settings of a tenant: the human-like delay and the feature flags (image_to_text)."""
    tenant_name = StringField(required=True)
    chatbot_human_like_delay = BooleanField(default=False)
    chatbot_human_like_time = IntField(default=0)
    features = ListField(EmbeddedDocumentField(FeatureSettings))
    meta = {"collection": "chat_settings"}

    def ensure_feature(self, feature_name: str) -> FeatureSettings:
        """The feature by name; a missing one is created from its default config and saved."""
        feature = next((candidate for candidate in self.features if candidate.name == feature_name), None)
        if not feature:
            feature_defaults = self.resolve_feature_defaults(feature_name)

            feature = _build_feature_flag_from_defaults(feature_name, feature_defaults)

            self.features.append(feature)
            self.save()
        return feature

    @classmethod
    def resolve_feature_defaults(cls, feature_name: str) -> dict:
        """Default definition of a chat feature (image_to_text); unknown names get a disabled stub."""
        known_feature_defaults = {
            "image_to_text": {
                "enabled": False,
                "meta_data": {},
                "options": {},
                "description": "Convert images to text using OCR",
                "he_description": "המרת תמונות לטקסט באמצעות OCR",
            },
        }

        return known_feature_defaults.get(
            feature_name,
            {
                "enabled": False,
                "meta_data": {},
                "options": {},
                "description": f"Feature: {feature_name}",
                "he_description": f"תכונה: {feature_name}",
            },
        )

    def backfill_feature_defaults(self):
        """Backfill the known chat features and their options / descriptions; returns True when something was saved.
        """
        has_changes = False
        known_feature_names = ["image_to_text"]

        stored_flags_by_name = {feature.name: feature for feature in self.features}

        for feature_name in known_feature_names:
            feature_defaults = self.resolve_feature_defaults(feature_name)

            if feature_name in stored_flags_by_name:
                stored_flag = stored_flags_by_name[feature_name]
                if _align_feature_flag_with_defaults(stored_flag, feature_defaults):
                    has_changes = True

            else:
                added_flag = _build_feature_flag_from_defaults(feature_name, feature_defaults)
                self.features.append(added_flag)
                has_changes = True

        if has_changes:
            try:
                self.save()
                return True
            except Exception as error:
                logger.error(f"Error saving {self.tenant_name}: {str(error)}")
                return False
        else:
            return False

class realtimeConfModel(EmbeddedModel):
    """Realtime session tuning (odmantic / API view): VAD padding and silence, transcription model and language,
    noise reduction. The VAD threshold is fixed in ``maia/voice/session.py``.
    """
    prefix_padding_ms: int = Field(
        default=600, ge=0, le=2000, description="Padding in milliseconds before speech starts"
    )
    silence_duration_ms: int = Field(
        default=1200, ge=0, le=2000, description="Duration in milliseconds to consider silence"
    )
    audio_transcription_model: str = Field(default="gpt-4o-transcribe", description="Model for audio transcription")
    audio_transcription_language: Optional[str] = Field(default=None, description="Language for audio transcription")
    noise_reduction_mode: Optional[str] = Field(default="near_field", description="Noise reduction mode")


class CallSettings(Model):
    """Call settings as the API sees them (odmantic model)."""
    tenant_name: str
    phone_number: str = ""
    # The phone channel runs only on the OpenAI Realtime media stream.
    tts_provider: Literal["realtime"] = "realtime"
    gpt_voice: str = "marin"  # same default as CallSettingsDocument
    openai_speed: float = Field(default=1.0)
    realtime_conf: realtimeConfModel = Field(default_factory=realtimeConfModel)


class RealtimeConf(EmbeddedDocument):
    """Realtime session tuning stored in Mongo (VAD padding and silence, transcription, noise reduction)."""
    prefix_padding_ms: int = IntField(
        default=600, min_value=0, max_value=2000, description="Padding in milliseconds before speech starts"
    )
    silence_duration_ms: int = IntField(
        default=1200, min_value=0, max_value=2000, description="Duration in milliseconds to consider silence"
    )
    audio_transcription_model: str = StringField(default="gpt-4o-transcribe", description="Model for audio transcription")
    audio_transcription_language: Optional[str] = StringField(description="Language for audio transcription")
    noise_reduction_mode: Optional[str] = StringField(default="near_field", choices=("near_field", "far_field", "none"))
    # strict=False like CallSettingsDocument: a stored key this model no longer defines does not break the load.
    meta = {"strict": False}


class CallSettingsDocument(Document):
    """Call settings of a tenant: the business number and the Realtime voice / speed / tuning."""
    tenant_name = StringField(required=True)
    phone_number = StringField(default="")
    # OpenAI Realtime voice + speed (read by maia/voice/session.py)
    gpt_voice = StringField(default="marin")
    openai_speed = FloatField(default=1.0)
    realtime_conf = EmbeddedDocumentField(RealtimeConf, default=RealtimeConf())
    # The phone channel runs on the OpenAI Realtime media stream.
    tts_provider = StringField(default="realtime", choices=["realtime"])
    # strict=False: ignore fields on stored documents that this model does not define.
    meta = {"collection": "call_settings", "strict": False}
