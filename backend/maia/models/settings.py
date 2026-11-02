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


class ChatSettingsDocument(Document):
    """Chat settings of a tenant: the human-like delay and the feature flags (image_to_text)."""
    tenant_name = StringField(required=True)
    chatbot_human_like_delay = BooleanField(default=False)
    chatbot_human_like_time = IntField(default=0)
    features = ListField(EmbeddedDocumentField(FeatureSettings))
    meta = {"collection": "chat_settings"}

    def get_feature(self, feature_name: str) -> FeatureSettings:
        """The feature by name; a missing one is created from its default config and saved."""
        feature = next((feature for feature in self.features if feature.name == feature_name), None)
        if not feature:
            default_config = self.get_default_feature_config(feature_name)

            feature = FeatureSettings(
                name=feature_name,
                enabled=default_config["enabled"],
                meta_data=default_config["meta_data"],
                options=default_config["options"],
                description=default_config["description"],
                he_description=default_config["he_description"],
            )

            self.features.append(feature)
            self.save()
        return feature

    @classmethod
    def get_default_feature_config(cls, feature_name: str) -> dict:
        """Default definition of a chat feature (image_to_text); unknown names get a disabled stub."""
        default_configs = {
            "image_to_text": {
                "enabled": False,
                "meta_data": {},
                "options": {},
                "description": "Convert images to text using OCR",
                "he_description": "המרת תמונות לטקסט באמצעות OCR",
            },
        }

        return default_configs.get(
            feature_name,
            {
                "enabled": False,
                "meta_data": {},
                "options": {},
                "description": f"Feature: {feature_name}",
                "he_description": f"תכונה: {feature_name}",
            },
        )

    def update_all_features(self):
        """Backfill the known chat features and their options / descriptions; returns True when something was saved.
        """
        updated = False
        all_default_features = ["image_to_text"]

        existing_features = {feature.name: feature for feature in self.features}

        for feature_name in all_default_features:
            default_config = self.get_default_feature_config(feature_name)

            if feature_name in existing_features:
                existing_feature = existing_features[feature_name]
                feature_updated = False
                if not hasattr(existing_feature, "options") or existing_feature.options is None:
                    existing_feature.options = {}
                    feature_updated = True

                # Update options: always sync with defaults (override if different)
                for key, value in default_config["options"].items():
                    if key not in existing_feature.options or existing_feature.options[key] != value:
                        existing_feature.options[key] = value
                        feature_updated = True

                if not hasattr(existing_feature, "description") or not existing_feature.description:
                    existing_feature.description = default_config["description"]
                    feature_updated = True

                if not hasattr(existing_feature, "he_description") or not existing_feature.he_description:
                    existing_feature.he_description = default_config["he_description"]
                    feature_updated = True

                if feature_updated:
                    updated = True

            else:
                new_feature = FeatureSettings(
                    name=feature_name,
                    enabled=default_config["enabled"],
                    meta_data=default_config["meta_data"],
                    options=default_config["options"],
                    description=default_config["description"],
                    he_description=default_config["he_description"],
                    meta_help=default_config.get("meta_help", {}),
                )
                self.features.append(new_feature)
                updated = True

        if updated:
            try:
                self.save()
                return True
            except Exception as e:
                logger.error(f"Error saving {self.tenant_name}: {str(e)}")
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
