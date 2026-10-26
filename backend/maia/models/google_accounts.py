"""GoogleShared: a Google account connected to a tenant (calendar / Gmail tokens, opening hours, Gmail watch flag)."""

from enum import Enum

from mongoengine import (
    BooleanField,
    DateTimeField,
    Document,
    EmbeddedDocument,
    EmbeddedDocumentField,
    ListField,
    StringField,
)
from pydantic import BaseModel


class OpeningHours(EmbeddedDocument):
    day = StringField(required=True)
    start_time = StringField(required=False)
    end_time = StringField(required=False)
    closed = BooleanField(default=False)


DAYS_OF_WEEK = [
    OpeningHours(day="sun", start_time="09:00", end_time="17:00", closed=False),
    OpeningHours(day="mon", start_time="09:00", end_time="17:00", closed=False),
    OpeningHours(day="tue", start_time="09:00", end_time="17:00", closed=False),
    OpeningHours(day="wed", start_time="09:00", end_time="17:00", closed=False),
    OpeningHours(day="thu", start_time="09:00", end_time="17:00", closed=False),
    OpeningHours(day="fri", start_time="09:00", end_time="13:00", closed=False),
    OpeningHours(day="sat", closed=True),
]


class GoogleShared(Document):
    meta = {"collection": "google_accounts", "indexes": [{"fields": ["email"], "unique": True, "sparse": True}]}

    tenant_name = StringField(required=True)
    email = StringField(required=True, unique=True)
    access_token = StringField(required=False)
    refresh_token = StringField(required=False)
    expires_at = DateTimeField(required=False)
    default = BooleanField(default=False)
    opening_hours = ListField(EmbeddedDocumentField(OpeningHours), default=DAYS_OF_WEEK)
    # If True, ignore events longer than 12 hours (like all-day reminders)
    ignore_long_events = BooleanField(default=False)
    # List of authorized OAuth scopes for this calendar connection
    scopes = ListField(StringField(), required=False)
    created_at = DateTimeField(required=False)
    # If True, the Gmail watch (pub/sub) will be renewed automatically every ~3 days
    gmail_watch_enabled = BooleanField(default=False)


class DaysOfWeek(str, Enum):
    SUN = "sun"
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"


class OpeningHoursModel(BaseModel):
    day: DaysOfWeek
    start_time: str
    end_time: str
    closed: bool

    class Config:
        json_schema_extra = {"example": {"day": "mon", "start_time": "09:00", "end_time": "17:00", "closed": False}}
