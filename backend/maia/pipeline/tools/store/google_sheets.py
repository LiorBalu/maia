"""Google Sheets lookup tool: the bot reads the tenant's connected spreadsheet in real time.

A tenant connects a sheet on the dashboard (Settings > Google Sheets; stored in
``TenantDocument.google_sheet_id``) and its ``tenants/<folder>/handling.py`` appends
``st_find_sheet_rows`` to the request's ``tool_names``.
"""

import logging
from maia.models.tenants import TenantDocument
from maia.services.google_sheets import SheetAccessError, SheetData, render_rows_as_text, fetch_sheet_data, find_matching_rows
from maia.pipeline.tools.function_decorator import llm_tool
from maia.pipeline.context import MessageContext

logger = logging.getLogger(__name__)

NO_SHEET_CONNECTED_RESULT = (
    "No Google Sheet is connected for this business. Answer from the knowledge documents and say that "
    "availability will be confirmed by a representative."
)


def _load_connected_sheet_id(tenant_name: str) -> str:
    """The id of the spreadsheet the tenant connected; '' when the tenant does not exist or has no sheet connected."""
    tenant_doc = TenantDocument.objects(name=tenant_name).only("google_sheet_id").first()
    return (tenant_doc.google_sheet_id if tenant_doc else "") or ""


def _compose_search_result(sheet_data: SheetData, query: str, matching_rows: list[dict[str, str]]) -> str:
    """The text handed back to the model: the matching rows with the sheet's title and columns, or a no-match note."""
    columns_text = ", ".join(sheet_data.column_headers)
    if not matching_rows:
        return (
            f"No row in the sheet '{sheet_data.spreadsheet_title}' matches '{query}' (columns: {columns_text}; {len(sheet_data.data_rows)} rows). "
            "Answer from the catalog documents and say that availability will be confirmed by a representative."
        )
    return f"Matching rows from the sheet '{sheet_data.spreadsheet_title}' (columns: {columns_text}):\n{render_rows_as_text(matching_rows)}"


@llm_tool
async def st_find_sheet_rows(request: MessageContext, query: str) -> str:
    """
    Looks up products, prices and stock in the business's Google Sheet (its live inventory list) and returns the matching rows. Call it for any question about a product's price or availability, or to list a category (e.g. all laptops) with prices.

    query: product name, SKU / model, keyword or category to look up (Hebrew or English), e.g. "Sony WH-1000XM5" or "מחשב נייד"
    """
    tenant_name = request["tenant_name"]
    connected_sheet_id = _load_connected_sheet_id(tenant_name)
    if not connected_sheet_id:
        logger.info(f"[st_find_sheet_rows] {tenant_name}: no sheet connected")
        return NO_SHEET_CONNECTED_RESULT

    try:
        sheet_data = fetch_sheet_data(connected_sheet_id)
    except SheetAccessError as access_error:
        logger.error(f"[st_find_sheet_rows] {tenant_name}: {access_error}")
        return f"The sheet could not be read: {access_error} Answer from the knowledge documents."
    except Exception as unexpected_error:  # network / API errors: never break the conversation
        logger.error(f"[st_find_sheet_rows] {tenant_name}: unexpected error: {unexpected_error}")
        return f"The sheet could not be read ({unexpected_error}). Answer from the knowledge documents."

    matching_rows = find_matching_rows(sheet_data.data_rows, query, max_matches=10)
    logger.info(f"[st_find_sheet_rows] {tenant_name}: '{query}' -> {len(matching_rows)} of {len(sheet_data.data_rows)} rows")
    return _compose_search_result(sheet_data, query, matching_rows)
