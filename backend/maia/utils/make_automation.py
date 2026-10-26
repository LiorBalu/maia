"""Bootstraps a new tenant with an empty default prompt and a chat-settings document."""

from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument
from maia.models.settings import ChatSettingsDocument


def make_automation(tenant_doc: TenantDocument):
    """Bootstrap a new tenant: an empty default prompt and a chat-settings document (both idempotent)."""
    if not PromptDocument.objects(tenant_name=tenant_doc.name).first():
        PromptDocument(tenant_name=tenant_doc.name, name="default", content="").save()
    if not ChatSettingsDocument.objects(tenant_name=tenant_doc.name).first():
        ChatSettingsDocument(tenant_name=tenant_doc.name).save()
