"""Time-of-day label (morning / noon / evening) used in prompts."""

from datetime import datetime, time, timezone
from typing import Optional


def get_time_of_day(current_datetime: Optional[datetime] = None) -> str:
    """morning / noon / afternoon / evening / night for a datetime (now by default)."""
    current_time = (current_datetime or datetime.now(timezone.utc)).time()

    if time(5, 0) <= current_time < time(12, 0):
        return "morning"
    elif time(12, 0) <= current_time < time(13, 0):
        return "noon"
    elif time(13, 0) <= current_time < time(17, 0):
        return "afternoon"
    elif time(17, 0) <= current_time < time(21, 0):
        return "evening"
    else:
        return "night"
