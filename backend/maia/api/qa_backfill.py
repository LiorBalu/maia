"""One-off backfill that assigns a category to Q&A pairs created before categories existed (runs lazily from the Q&A API)."""

from __future__ import annotations

from typing import Set

from maia.models.qa import QADocument

_backfilled_tenants: Set[str] = set()


def ensure_qa_categories_backfilled(tenant_name: str) -> None:
    """
    Lazy, idempotent backfill for older Q&A docs missing category.

    Runs at the beginning of Q&A read endpoints, scoped per tenant.
    Blocks the first request per worker per tenant; subsequent requests are no-ops.
    """
    if not tenant_name:
        return

    # Per-process cache to avoid repeated updateMany calls.
    if tenant_name in _backfilled_tenants:
        return

    # Missing field
    QADocument.objects(tenant_name=tenant_name, category__exists=False).update(
        set__category="general"
    )
    # Null / empty string
    QADocument.objects(tenant_name=tenant_name, category__in=[None, ""]).update(
        set__category="general"
    )

    _backfilled_tenants.add(tenant_name)


