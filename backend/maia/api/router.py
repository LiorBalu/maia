"""The `/management/v1` router: mounts every admin API module."""

from fastapi import APIRouter

from maia.api.agents import agents_router
from maia.api.calendar import calendar_router
from maia.api.customers import customer_router
from maia.api.email import email_router
from maia.api.knowledge_documents import embedded_documents_router, embedded_parts_router
from maia.api.messages import conversations_router, messages_router
from maia.api.waha import router as waha_router
from maia.api.prompts import prompts_router
from maia.api.qa import qa_router
from maia.api.settings import settings_router
from maia.api.sheets import sheets_router
from maia.api.tenants import tenant_router
from maia.api.tools import tools_router

router = APIRouter()


router.include_router(agents_router)
router.include_router(calendar_router)
router.include_router(conversations_router)
router.include_router(customer_router)
router.include_router(email_router)
router.include_router(embedded_documents_router)
router.include_router(embedded_parts_router)
router.include_router(messages_router)
router.include_router(waha_router)
router.include_router(prompts_router)
router.include_router(qa_router)
router.include_router(settings_router)
router.include_router(sheets_router)
router.include_router(tenant_router)
router.include_router(tools_router)
