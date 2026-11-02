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
    created_date = DateTimeField(required=False, default=datetime.now(timezone.utc).replace(microsecond=0))
    updated_date = DateTimeField(required=False)
    meta_data = DictField(default=dict)
    meta = {"collection": "customers"}

    def save(self, *args, **kwargs):
        """
        Override save method to set created_date and updated_date."""
        self.created_date = self.created_date or datetime.now(timezone.utc).replace(microsecond=0)
        self.updated_date = datetime.now(timezone.utc).replace(microsecond=0)
        if self.platform is None or self.platform == "":
            from maia.models.tenants import TenantDocument

            tenant = TenantDocument.objects(name=self.tenant_name).first()
            if tenant:
                self.platform = tenant.get_feature_by_name("default_platform").meta_data.get("platform")
        return super(CustomerDocument, self).save(*args, **kwargs)


def fetch_or_create_customer(
    number_phone: str,
    tenant_name: str,
    text: Optional[str] = None,
    profile_name: str = "",
    platform: Optional[str] = None,
) -> CustomerDocument:
    """Find the customer by phone number or email within the tenant, or create it; an empty name is filled with
    profile_name.
    """
    customer = CustomerDocument.objects(phone_number=number_phone, tenant_name=tenant_name).first()
    if not customer:
        customer = CustomerDocument.objects(email=number_phone, tenant_name=tenant_name).first()
    if not customer:
        customer = CustomerDocument(
            tenant_name=tenant_name,
            phone_number=number_phone,
            name=profile_name,
            platform=platform,
            created_date=datetime.now(timezone.utc).replace(microsecond=0),
        )
        customer.save()
        customer.reload()
    if not customer.name or customer.name == "":
        customer.name = profile_name
    customer.save()
    return customer
