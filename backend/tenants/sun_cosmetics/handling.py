"""Sun Cosmetics (tenant `sun-cosmetics`): beauty salon in Tel Aviv.

The secretary answers from the knowledge base (price list, FAQ and policies) and books,
reschedules and cancels appointments in the salon's Google Calendar.
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


async def prepare_context_for_sun_cosmetics(message_context: MessageContext) -> None:
    """Expose the calendar tools on every turn (chat and phone)."""
    for tool_name in CALENDAR_TOOL_NAMES:
        if tool_name not in message_context["tool_names"]:
            message_context["tool_names"].append(tool_name)
