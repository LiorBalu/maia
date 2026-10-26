"""Chat settings (delay, features) and call settings (voice, speed, Realtime tuning, noise suppression) of a tenant."""

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
    name = StringField(required=True)
    enabled = BooleanField(default=False)
    meta_data = DictField(default={})
    options = DictField(default=dict)
    description = StringField(default="")
    he_description = StringField(default="")
    # Optional per-field help text. Same shape as FeatureSettingsModel.meta_help.
    meta_help = DictField(default=dict)


class ChatSettingsDocument(Document):
    tenant_name = StringField(required=True)
    chatbot_human_like_delay = BooleanField(default=False)
    chatbot_human_like_time = IntField(default=0)
    features = ListField(EmbeddedDocumentField(FeatureSettings))
    meta = {"collection": "chat_settings"}

    def get_feature(self, feature_name: str) -> FeatureSettings:
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
        default_configs = {
            "image_to_text": {
                "enabled": False,
                "meta_data": {},
                "options": {},
                "description": "Convert images to text using OCR",
                "he_description": "המרת תמונות לטקסט בםמצעות OCR",
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
    threshold: float = Field(
        default=0.4, ge=0.0, le=1.0, description="Threshold for real-time voice activity detection"
    )
    prefix_padding_ms: int = Field(
        default=600, ge=0, le=2000, description="Padding in milliseconds before speech starts"
    )
    silence_duration_ms: int = Field(
        default=1200, ge=0, le=2000, description="Duration in milliseconds to consider silence"
    )
    temperature: float = Field(default=0.6, ge=0.6, le=1.2, description="Temperature for response generation")
    audio_transcription_model: str = Field(default="gpt-4o-transcribe", description="Model for audio transcription")
    audio_transcription_language: Optional[str] = Field(default=None, description="Language for audio transcription")
    noise_reduction_mode: Optional[str] = Field(default="near_field", description="Noise reduction mode")


class CallSettings(Model):
    tenant_name: str
    phone_number: str = ""
    representative_phone_number: str = ""
    # The phone channel runs only on the OpenAI Realtime media stream.
    tts_provider: Literal["realtime"] = "realtime"
    gpt_voice: str = "alloy"
    openai_speed: float = Field(default=1.0)
    realtime_conf: realtimeConfModel = Field(default_factory=realtimeConfModel)
    features: Optional[List[FeatureSettingsModel]] = []


class RealtimeConf(EmbeddedDocument):
    threshold: float = FloatField(
        default=0.4, min_value=0.0, max_value=1.0, description="Threshold for real-time voice activity detection"
    )
    prefix_padding_ms: int = IntField(
        default=600, min_value=0, max_value=2000, description="Padding in milliseconds before speech starts"
    )
    silence_duration_ms: int = IntField(
        default=1200, min_value=0, max_value=2000, description="Duration in milliseconds to consider silence"
    )
    temperature: float = FloatField(
        default=0.6, min_value=0.6, max_value=1.2, description="Temperature for response generation"
    )
    audio_transcription_model: str = StringField(default="gpt-4o-transcribe", description="Model for audio transcription")
    audio_transcription_language: Optional[str] = StringField(description="Language for audio transcription")
    noise_reduction_mode: Optional[str] = StringField(default="near_field", choices=("near_field", "far_field", "none"))


class CallSettingsDocument(Document):
    tenant_name = StringField(required=True)
    phone_number = StringField(default="")
    representative_phone_number = StringField(default="")
    # OpenAI Realtime voice + speed (read by maia/voice/session.py)
    gpt_voice = StringField(default="marin")
    openai_speed = FloatField(default=1.0)
    realtime_conf = EmbeddedDocumentField(RealtimeConf, default=RealtimeConf())
    # The phone channel runs on the OpenAI Realtime media stream.
    tts_provider = StringField(default="realtime", choices=["realtime"])
    features = ListField(EmbeddedDocumentField(FeatureSettings))
    # strict=False: ignore fields on stored documents that this model does not define.
    meta = {"collection": "call_settings", "strict": False}

    def get_feature(self, feature_name: str) -> FeatureSettings:
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
        default_configs = {
            "noise_suppression": {
                "enabled": False,
                "meta_data": {
                    # Strong defaults tuned for Twilio μ-law 8 kHz + the OpenAI Realtime VAD.
                    # Edit this JSON to adjust without a code change.
                    "noise_suppression_level": 3,
                    "vad_gate_enabled": True,
                    # Require both 10ms speech-positive APM chunks inside the
                    # current 20ms telephony frame before opening the gate.
                    # This is the main protection against short background-noise
                    # bursts being treated as speech.
                    "speech_subframes_to_open": 2,
                    # Require this many consecutive speech-positive 20ms frames
                    # before opening the gate. Prevents tiny noise bursts from
                    # reopening the stream.
                    "speech_frames_to_open": 1,
                    "vad_hangover_ms": 500,
                    # Extra RMS-based floor gate catches steady low-level noise that
                    # WebRTC VAD sometimes lets through. 0 disables.
                    "rms_floor_dbfs": -45,
                    "auto_gain_enabled": False,
                    "auto_gain_dbfs": -3,
                },
                "options": {
                    "noise_suppression_level": [0, 1, 2, 3, 4],
                    "vad_gate_enabled": [True, False],
                    "speech_subframes_to_open": [1, 2],
                    "speech_frames_to_open": [1, 2, 3, 4],
                    "vad_hangover_ms": [0, 200, 400, 500, 700, 1000],
                    "rms_floor_dbfs": [0, -30, -40, -45, -50, -55],
                    "auto_gain_enabled": [True, False],
                    "auto_gain_dbfs": [-31, -10, -6, -3, 0, 6],
                },
                "meta_help": {
                    "noise_suppression_level": {
                        "en": "Denoiser strength. 0 = off, 4 = strongest. Too high can damage speech. Recommended: 3.",
                        "he": "עוצמת סינון הרעש. 0 = כבוי, 4 = הכי חזק. גבוה מדי יכול לפגוע בדיבור. מומלץ: 3.",
                    },
                    "vad_gate_enabled": {
                        "en": "Silences non-speech frames before they reach the AI. Most important setting for avoiding background-noise interruptions.",
                        "he": "משתיק קטעים ללא דיבור לפני שמגיעים ל-AI. ההגדרה הכי חשובה למניעת הפרעות מרעשי רקע.",
                    },
                    "speech_subframes_to_open": {
                        "en": "How many 10ms speech-positive chunks inside the current 20ms telephony frame are required before opening the gate. 2 is stricter and better at rejecting short noise bursts.",
                        "he": "כמה חלקי 10ms עם דיבור בתוך פריים הטלפוניה של 20ms נדרשים כדי לפתוח את השער. 2 הוא מחמיר יותר וטוב יותר נגד פרצי רעש קצרים.",
                    },
                    "speech_frames_to_open": {
                        "en": "How many consecutive 20ms speech frames are required before opening the gate. Higher = fewer false openings from noise, but may clip the start of speech. Recommended: 1 when subframe gating is enabled.",
                        "he": "כמה פריימים רצופים של 20ms עם דיבור נדרשים כדי לפתוח את השער. גבוה יותר = פחות פתיחות שווא מרעש, אבל עלול לחתוך את תחילת הדיבור. מומלץ: 1 כאשר יש סינון לפי תת-פריימים.",
                    },
                    "vad_hangover_ms": {
                        "en": "How long to keep the gate open after speech ends, so word endings are not cut. Higher = less clipping, more noise passes. Default: 500.",
                        "he": "כמה זמן להשאיר את השער פתוח אחרי דיבור, כדי לא לחתוך סופי מילים. גבוה יותר = פחות חיתוך אבל יותר רעש עובר. ברירת מחדל: 500.",
                    },
                    "rms_floor_dbfs": {
                        "en": "Volume floor. Anything quieter than this is muted. Closer to 0 = more aggressive. Default: -45.",
                        "he": "רצפת עוצמה. כל מה שחלש מזה יושתק. קרוב יותר ל-0 = אגרסיבי יותר. ברירת מחדל: -45.",
                    },
                    "auto_gain_enabled": {
                        "en": "Automatically boosts quiet speech. Usually best OFF on phone audio because it also boosts background noise.",
                        "he": "מגביר אוטומטית דיבור חלש. בדרך כלל עדיף כבוי בטלפוניה כי זה מגביר גם רעשי רקע.",
                    },
                    "auto_gain_dbfs": {
                        "en": "Target loudness for automatic gain when AGC is on. Usually does not need to change.",
                        "he": "יעד העוצמה עבור ההגברה האוטומטית, אם היא מופעלת. בדרך כלל אין צורך לשנות.",
                    },
                },
                "description": "Filter background noise on incoming audio before the AI hears it (reduces false interruptions).",
                "he_description": "סינון רעשי רקע באודיו הנכנס לפני שה-AI שומע (מצמצם הפרעות שגויות).",
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
        updated = False
        all_default_features = ["noise_suppression"]

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

                # Backfill meta_data with defaults when keys are missing, so the
                # UI has something editable on first load. Preserves any existing
                # per-tenant edits.
                if default_config.get("meta_data"):
                    if not hasattr(existing_feature, "meta_data") or existing_feature.meta_data is None:
                        existing_feature.meta_data = {}
                        feature_updated = True
                    for key, value in default_config["meta_data"].items():
                        if key not in existing_feature.meta_data:
                            existing_feature.meta_data[key] = value
                            feature_updated = True

                # Always sync meta_help (pure UI text — never user-edited).
                if default_config.get("meta_help"):
                    if getattr(existing_feature, "meta_help", None) != default_config["meta_help"]:
                        existing_feature.meta_help = default_config["meta_help"]
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
                logger.error(f"Error saving call settings for {self.tenant_name}: {str(e)}")
                return False
        else:
            return False
