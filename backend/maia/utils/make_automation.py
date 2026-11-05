"""Bootstraps a new tenant with an empty default prompt (the settings documents are created by the tenants API)."""

from maia.models.prompts import PromptDocument
from maia.models.tenants import TenantDocument


def seed_default_prompt(tenant_doc: TenantDocument):
    """Bootstrap a new tenant: an empty default prompt (idempotent)."""
    if not PromptDocument.objects(tenant_name=tenant_doc.name).first():
        PromptDocument(tenant_name=tenant_doc.name, name="default", content="").save()
