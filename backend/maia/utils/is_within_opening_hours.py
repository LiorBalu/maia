"""Checks a datetime against a calendar's opening hours."""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _is_open_within_day_hours(day_hours, moment: datetime) -> bool:
    """True when *moment* is inside *day_hours*: a closed day is False, equal start and end mean open all day (24h)."""
    if day_hours.closed:
        logger.debug(f"is_within_opening_hours: {day_hours.day} is marked as closed")
        return False  # The day is closed
    opens_at = datetime.strptime(day_hours.start_time, "%H:%M").time()
    closes_at = datetime.strptime(day_hours.end_time, "%H:%M").time()
    time_of_day = moment.time()
    # If start and end are both 00:00, the day is open 24h
    if opens_at == closes_at:
        is_open = True
    else:
        is_open = opens_at <= time_of_day <= closes_at
    logger.debug(
        f"is_within_opening_hours: {day_hours.day} hours {day_hours.start_time}-{day_hours.end_time}, check_time={time_of_day}, in_range={is_open}"
    )
    return is_open


def is_datetime_within_opening_hours(point_in_time, weekly_opening_hours):
    """
    Check if a given datetime is within the opening hours of a tenant.
    :param point_in_time: The datetime to check
    :param weekly_opening_hours: A list of OpeningHours objects
    :return: True if the datetime is within the opening hours, False otherwise
    """
    weekday_abbreviation = point_in_time.strftime("%a").lower()  # Operating hours are stored as abbreviated day names

    # Log debug info
    logger.debug(f"is_within_opening_hours: checking {point_in_time}, day_name={weekday_abbreviation}")
    logger.debug(f"is_within_opening_hours: opening_hours has {len(weekly_opening_hours) if weekly_opening_hours else 0} entries")

    for day_hours in weekly_opening_hours:
        if day_hours.day == weekday_abbreviation:
            return _is_open_within_day_hours(day_hours, point_in_time)

    logger.warning(
        f"is_within_opening_hours: day {weekday_abbreviation} NOT FOUND in opening_hours! Available days: {[day_hours.day for day_hours in weekly_opening_hours]}"
    )
    return False
