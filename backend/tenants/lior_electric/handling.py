"""Lior Electric (tenant `lior-electric`): electronics store in Haifa.

The secretary answers from the knowledge base (catalog, warranty, returns and delivery
policies), looks prices and stock up in the store's Google Sheet, and books appointments
for the repair lab, in-store consultation and order pickup in the store's Google Calendar.
"""

from maia.pipeline.context import MessageContext

CALENDAR_TOOL_NAMES = [
    "st_list_upcoming_free_slots",
    "st_list_free_slots_in_range",
    "st_check_slot_availability",
    "st_book_appointment",
    "st_find_customer_appointments",
    "st_reschedule_appointment",
    "st_cancel_appointment",
]

# Live price / stock lookup in the sheet connected on the dashboard (Settings > Google Sheets).
SHEET_TOOL_NAMES = ["st_find_sheet_rows"]


async def prepare_context_for_lior_electric(message_context: MessageContext) -> None:
    """Expose the calendar and sheet tools on every turn (chat and phone)."""
    for tool_name in CALENDAR_TOOL_NAMES + SHEET_TOOL_NAMES:
        if tool_name not in message_context["tool_names"]:
            message_context["tool_names"].append(tool_name)
