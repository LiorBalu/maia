"""Feature flags embedded in a tenant document, with their defaults."""

from mongoengine import StringField, BooleanField, DictField, EmbeddedDocument
from typing import Dict
from pydantic import BaseModel, Field

# ------------------------------- Pydentic model -------------------------------------------- #


class FeatureTenantModel(BaseModel):
    """A tenant feature flag as the API sees it."""
    name: str
    enabled: bool = False
    meta_data: Dict = Field(default={})


# ------------------------------- Document model -------------------------------------------- #


class FeatureTenant(EmbeddedDocument):
    """A tenant feature flag stored in the tenant document (default_platform, call_auto_reset)."""
    name = StringField(required=True)
    enabled = BooleanField(default=False)
    meta_data = DictField(default={})


DEFAULT_TENANT_FEATURES = [
    FeatureTenant(name="default_platform", enabled=True, meta_data={"platform": "waha"}),
    FeatureTenant(name="call_auto_reset", enabled=True, meta_data={"on_start": True, "on_end": True}),
]
