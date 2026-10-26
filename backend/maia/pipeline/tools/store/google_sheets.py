"""Google Sheets lookup tool: the bot reads the tenant's connected spreadsheet in real time.

A tenant connects a sheet on the dashboard (Settings > Google Sheets; stored in
``TenantDocument.google_sheet_id``) and its ``tenants/<folder>/handling.py`` appends
``st_search_sheet`` to the request's ``tool_names``.
"""

import logging
from maia.models.tenants import TenantDocument
from maia.services.google_sheets import SheetAccessError, format_rows, read_sheet, search_rows
from maia.pipeline.tools.function_decorator import gpt_tool
from maia.pipeline.context import MessageContext

logger = logging.getLogger(__name__)

NO_SHEET = (
    "No Google Sheet is connected for this business. Answer from the knowledge documents and say that "
    "availability will be confirmed by a representative."
)


@gpt_tool
async def st_search_sheet(request: MessageContext, query: str) -> str:
    """
    Looks up products, prices and stock in the business's Google Sheet (its live inventory list) and returns the matching rows. Call it for any question about a product's price or availability, or to list a category (e.g. all laptops) with prices.

    query: product name, SKU / model, keyword or category to look up (Hebrew or English), e.g. "Sony WH-1000XM5" or "מחשב נייד"
    """
    tenant_name = request["tenant_name"]
    tenant = TenantDocument.objects(name=tenant_name).only("google_sheet_id").first()
    sheet_id = (tenant.google_sheet_id if tenant else "") or ""
    if not sheet_id:
        logger.info(f"[st_search_sheet] {tenant_name}: no sheet connected")
        return NO_SHEET

    try:
        data = read_sheet(sheet_id)
    except SheetAccessError as exc:
        logger.error(f"[st_search_sheet] {tenant_name}: {exc}")
        return f"The sheet could not be read: {exc} Answer from the knowledge documents."
    except Exception as exc:  # network / API errors: never break the conversation
        logger.error(f"[st_search_sheet] {tenant_name}: unexpected error: {exc}")
        return f"The sheet could not be read ({exc}). Answer from the knowledge documents."

    matches = search_rows(data.rows, query, limit=10)
    columns = ", ".join(data.headers)
    logger.info(f"[st_search_sheet] {tenant_name}: '{query}' -> {len(matches)} of {len(data.rows)} rows")
    if not matches:
        return (
            f"No row in the sheet '{data.title}' matches '{query}' (columns: {columns}; {len(data.rows)} rows). "
            "Answer from the catalog documents and say that availability will be confirmed by a representative."
        )
    return f"Matching rows from the sheet '{data.title}' (columns: {columns}):\n{format_rows(matches)}"
