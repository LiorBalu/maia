"""Sun Cosmetics (tenant `sun-cosmetics`): beauty salon in Tel Aviv.

The secretary answers from the knowledge base (price list, FAQ and policies) and books,
reschedules and cancels appointments in the salon's Google Calendar.
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


async def handle_sun_cosmetics(request: MessageContext) -> None:
    """Expose the calendar tools on every turn (chat and phone)."""
    for tool in CALENDAR_TOOLS:
        if tool not in request["tool_names"]:
            request["tool_names"].append(tool)
