"""Checks a datetime against a calendar's opening hours."""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def is_within_opening_hours(check_datetime, opening_hours):
    """
    Check if a given datetime is within the opening hours of a tenant.
    :param check_datetime: The datetime to check
    :param opening_hours: A list of OpeningHours objects
    :return: True if the datetime is within the opening hours, False otherwise
    """
    day_name = check_datetime.strftime("%a").lower()  # Operating hours are stored as abbreviated day names

    # Log debug info
    logger.debug(f"is_within_opening_hours: checking {check_datetime}, day_name={day_name}")
    logger.debug(f"is_within_opening_hours: opening_hours has {len(opening_hours) if opening_hours else 0} entries")

    for oh in opening_hours:
        if oh.day == day_name:
            if oh.closed:
                logger.debug(f"is_within_opening_hours: {day_name} is marked as closed")
                return False  # The day is closed
            start_time = datetime.strptime(oh.start_time, "%H:%M").time()
            end_time = datetime.strptime(oh.end_time, "%H:%M").time()
            check_time = check_datetime.time()
            # If start and end are both 00:00, the day is open 24h
            if start_time == end_time:
                is_in_range = True
            else:
                is_in_range = start_time <= check_time <= end_time
            logger.debug(
                f"is_within_opening_hours: {day_name} hours {oh.start_time}-{oh.end_time}, check_time={check_time}, in_range={is_in_range}"
            )
            return is_in_range

    logger.warning(
        f"is_within_opening_hours: day {day_name} NOT FOUND in opening_hours! Available days: {[oh.day for oh in opening_hours]}"
    )
    return False
