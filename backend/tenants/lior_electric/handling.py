"""Lior Electric (tenant `lior-electric`): electronics store in Haifa.

The secretary answers from the knowledge base (catalog, warranty, returns and delivery
policies), looks prices and stock up in the store's Google Sheet, and books appointments
for the repair lab, in-store consultation and order pickup in the store's Google Calendar.
"""

from maia.pipeline.context import MessageContext

CALENDAR_TOOLS = [
    "st_get_available_event_times",
    "st_get_available_times_by_range",
    "st_check_availability_by_date_and_time",
    "st_schedule_event",
    "st_get_event_details",
    "st_reschedule_event",
    "st_cancel_event",
]

# Live price / stock lookup in the sheet connected on the dashboard (Settings > Google Sheets).
SHEET_TOOLS = ["st_search_sheet"]


async def handle_lior_electric(request: MessageContext) -> None:
    """Expose the calendar and sheet tools on every turn (chat and phone)."""
    for tool in CALENDAR_TOOLS + SHEET_TOOLS:
        if tool not in request["tool_names"]:
            request["tool_names"].append(tool)
