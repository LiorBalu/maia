"""CustomerDocument: an end user of a tenant (keyed by phone or email), with meetings, metadata flags and reply mode."""

from datetime import datetime, timezone
from typing import Optional

from mongoengine import (
    DateTimeField,
    DictField,
    Document,
    EmailField,
    EmbeddedDocument,
    EmbeddedDocumentField,
    ListField,
    StringField,
)


class MeetingDocument(EmbeddedDocument):
    """An appointment the bot booked for the customer (Google Calendar event id, link, times, status)."""
    meeting_id = StringField(required=True)
    meeting_url = StringField(required=True)
    start_time = DateTimeField(required=True)
    end_time = DateTimeField(required=True)
    status = StringField(default="confirmed")  # choices=["active", "completed", "cancelled", "recorded"]


def _truncate_utc_now_to_seconds() -> datetime:
    """The current UTC time with the microseconds zeroed (the precision of the customer timestamps)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


class CustomerDocument(Document):
    """An end user of a tenant: phone / email key, name, platform, reply mode (AI / agent / blocked), meetings and
    free-form meta_data.
    """
    tenant_name = StringField(required=True)
    name = StringField(required=True, default="")
    phone_number = StringField()
    avatar = StringField(required=False)
    email = EmailField(required=False)
    platform = StringField(required=True, default="")
    status = StringField(default="חדש")
    reply_mode = StringField(default="AI")
    meetings = ListField(EmbeddedDocumentField(MeetingDocument), default=list)
    created_date = DateTimeField(required=False, default=_truncate_utc_now_to_seconds())
    updated_date = DateTimeField(required=False)
    meta_data = DictField(default=dict)
    meta = {"collection": "customers"}

    def save(self, *args, **kwargs):
        """
        Override save method to set created_date and updated_date."""
        self.created_date = self.created_date or _truncate_utc_now_to_seconds()
        self.updated_date = _truncate_utc_now_to_seconds()
        _fill_platform_from_tenant_default(self)
        return super(CustomerDocument, self).save(*args, **kwargs)


def _fill_platform_from_tenant_default(customer_record: CustomerDocument) -> None:
    """When the customer's platform is None or empty, set it to the tenant's default_platform feature (only when the
    tenant document exists).

    May persist the tenant document: ensure_feature creates a missing default_platform feature and saves the tenant.
    """
    if customer_record.platform is None or customer_record.platform == "":
        from maia.models.tenants import TenantDocument

        tenant_doc = TenantDocument.objects(name=customer_record.tenant_name).first()
        if tenant_doc:
            customer_record.platform = tenant_doc.ensure_feature("default_platform").meta_data.get("platform")


def _find_customer_by_phone_or_email(phone_or_email: str, tenant_name: str) -> Optional[CustomerDocument]:
    """Find the tenant's customer by phone_number, then by email (both compared with phone_or_email); None when
    neither matches.
    """
    customer_record = CustomerDocument.objects(phone_number=phone_or_email, tenant_name=tenant_name).first()
    if not customer_record:
        customer_record = CustomerDocument.objects(email=phone_or_email, tenant_name=tenant_name).first()
    return customer_record


def _create_customer_record(
    phone_or_email: str, tenant_name: str, customer_profile_name: str, platform: Optional[str]
) -> CustomerDocument:
    """Create the tenant's customer keyed by phone_or_email, save it and return it reloaded from the database."""
    # An email address is stored in phone_number on purpose (the lookup key), as maia/inbound/save/email.py does.
    customer_record = CustomerDocument(
        tenant_name=tenant_name,
        phone_number=phone_or_email,
        name=customer_profile_name,
        platform=platform,
        created_date=_truncate_utc_now_to_seconds(),
    )
    customer_record.save()
    customer_record.reload()
    return customer_record


def ensure_customer(
    phone_or_email: str,
    tenant_name: str,
    message_text: Optional[str] = None,
    customer_profile_name: str = "",
    platform: Optional[str] = None,
) -> CustomerDocument:
    """Find the customer by phone number or email within the tenant, or create it; an empty name is filled with
    customer_profile_name.
    """
    customer_record = _find_customer_by_phone_or_email(phone_or_email, tenant_name)
    if not customer_record:
        customer_record = _create_customer_record(phone_or_email, tenant_name, customer_profile_name, platform)
    if not customer_record.name or customer_record.name == "":
        customer_record.name = customer_profile_name
    customer_record.save()
    return customer_record
