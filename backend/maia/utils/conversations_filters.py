"""Filtering helpers for inbox lists (status, platform, dates, text)."""

import logging
from typing import Optional
from datetime import datetime
from mongoengine.queryset.visitor import Q

logger = logging.getLogger(__name__)


def _normalize_to_day_start(moment: datetime) -> datetime:
    """The same calendar day at 00:00:00.000000."""
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _normalize_to_day_end(moment: datetime) -> datetime:
    """The same calendar day at 23:59:59.999999."""
    return moment.replace(hour=23, minute=59, second=59, microsecond=999999)


def _build_platform_clause(platform_filter: Optional[str]) -> Optional[Q]:
    """The platform clause of the inbox filter; None when no platform was selected."""
    if platform_filter == "whatsapp":
        # The inbox's "WhatsApp" filter is the WAHA channel ("whatsapp" is the UI's name for it).
        return Q(platform="waha")
    elif platform_filter:
        return Q(platform=platform_filter)
    return None


def _build_created_date_range_clause(
    created_from_time: Optional[datetime], created_until_time: Optional[datetime]
) -> Optional[Q]:
    """Whole-day range clause on the conversation's creation date; None when neither bound is set."""
    # All date filtering logic - only day level filtering (00:00 to 23:59)
    if created_from_time and created_until_time:
        # Both dates provided - filter conversations created within this date range
        created_range_start = _normalize_to_day_start(created_from_time)
        created_range_end = _normalize_to_day_end(created_until_time)
        return Q(date__gte=created_range_start, date__lte=created_range_end)
    elif created_from_time:
        # Only created_from_time provided - filter conversations created from start of this day onwards
        created_range_start = _normalize_to_day_start(created_from_time)
        return Q(date__gte=created_range_start)
    elif created_until_time:
        # Only created_until_time provided - filter conversations created up to end of this day
        created_range_end = _normalize_to_day_end(created_until_time)
        return Q(date__lte=created_range_end)
    return None


def _build_last_message_day_clause(last_message_time: Optional[datetime]) -> Optional[Q]:
    """Whole-day clause on the date of the conversation's last message; None when no day was selected."""
    if last_message_time:
        # Show conversations with last messages from 00:00 to 23:59 of the selected date
        last_message_day_start = _normalize_to_day_start(last_message_time)
        last_message_day_end = _normalize_to_day_end(last_message_time)
        return Q(last_message_date__gte=last_message_day_start, last_message_date__lte=last_message_day_end)
    return None


def build_conversation_filter_query(
    tenant_name: str,
    platform_filter: Optional[str] = None,
    created_from_day: Optional[str] = None,
    created_until_day: Optional[str] = None,
    phone_number_fragment: Optional[str] = None,
    conversation_status_filter: Optional[str] = None,
    read_by_manager_filter: Optional[bool] = None,
    last_message_day: Optional[str] = None,
    search_text: Optional[str] = None,
):
    """
    Constructs the query filters for fetching conversations from the database.
    """
    # Convert date filters to datetime objects - all dates work with full day ranges
    created_from_time = parse_filter_date(created_from_day)
    created_until_time = parse_filter_date(created_until_day)
    last_message_time = parse_last_message_filter_date(last_message_day)

    # Build query filters dynamically
    conversation_filter_query = Q(tenant_name=tenant_name)

    platform_clause = _build_platform_clause(platform_filter)
    if platform_clause is not None:
        conversation_filter_query &= platform_clause

    created_date_range_clause = _build_created_date_range_clause(created_from_time, created_until_time)
    if created_date_range_clause is not None:
        conversation_filter_query &= created_date_range_clause

    if phone_number_fragment:
        conversation_filter_query &= Q(phone_number__icontains=phone_number_fragment)
    if conversation_status_filter:
        conversation_filter_query &= Q(conversation_status=conversation_status_filter)
    if read_by_manager_filter is not None:
        conversation_filter_query &= Q(read_by_manager=read_by_manager_filter)
    # change message is not good for filtering conversations
    # if search_text:
    #     # If a query string is provided, filter conversations that match the query
    #     # This could be a text search or any other specific query logic
    #     conversation_filter_query &= Q(phone_number__icontains=search_text) | Q(message__icontains=search_text, profile_name__icontains=search_text)

    last_message_day_clause = _build_last_message_day_clause(last_message_time)
    if last_message_day_clause is not None:
        conversation_filter_query &= last_message_day_clause
    return conversation_filter_query


def _parse_datetime_text(date_text: str) -> datetime:
    """Parse the ISO date-and-time variants into a naive datetime; raises ValueError when none of them matches."""
    try:
        # Try ISO format with microseconds and timezone (e.g., "2024-01-01T23:59:59.999Z")
        date_with_utc_offset = date_text.replace("Z", "+00:00")
        return datetime.strptime(date_with_utc_offset, "%Y-%m-%dT%H:%M:%S.%f%z").replace(
            tzinfo=None
        )
    except ValueError:
        try:
            # Try ISO format with microseconds (e.g., "2024-01-01T23:59:59.999")
            return datetime.strptime(date_text, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            # Try ISO format without microseconds (e.g., "2024-01-01T23:59:59")
            return datetime.strptime(date_text, "%Y-%m-%dT%H:%M:%S")


def parse_last_message_filter_date(date_text) -> Optional[datetime]:
    """Parse a date string in one of the accepted formats (YYYY-MM-DD or ISO variants); None when invalid."""
    if not date_text:
        return None
    try:
        # First try simple date format (e.g., "2024-01-01") - this is the primary expected format
        return datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        try:
            return _parse_datetime_text(date_text)
        except ValueError:
            logger.error(f"ERROR   Invalid date format: {date_text}")
            return None


def parse_filter_date(date_text):
    """Parse a date string in one of the accepted formats (YYYY-MM-DD or ISO variants); None when invalid."""
    if not date_text:
        return None
    try:
        # First try simple date format (e.g., "2024-01-01") - this is the primary expected format
        return datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        try:
            return _parse_datetime_text(date_text)
        except ValueError:
            logger.error(f"ERROR   Invalid date format: {date_text}")
            return None
