"""Filtering helpers for inbox lists (status, platform, dates, text)."""

import logging
from typing import Optional
from datetime import datetime
from mongoengine.queryset.visitor import Q

logger = logging.getLogger(__name__)


def filter_conversations(
    tenant_name: str,
    platform: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    phone_number: Optional[str] = None,
    conversation_status: Optional[str] = None,
    read_by_manager: Optional[bool] = None,
    last_message_date: Optional[str] = None,
    query: Optional[str] = None,
):
    """
    Constructs the query filters for fetching conversations from the database.
    """
    # Convert date filters to datetime objects - all dates work with full day ranges
    from_date_dt = parse_date(from_date)
    to_date_dt = parse_date(to_date)
    last_message_date_dt = parse_last_message_date(last_message_date)

    # Build query filters dynamically
    query_filters = Q(tenant_name=tenant_name)

    if platform == "whatsapp":
        # The inbox's "WhatsApp" filter is the WAHA channel ("whatsapp" is the UI's name for it).
        query_filters &= Q(platform="waha")
    elif platform:
        query_filters &= Q(platform=platform)

    # All date filtering logic - only day level filtering (00:00 to 23:59)
    if from_date_dt and to_date_dt:
        # Both dates provided - filter conversations created within this date range
        from_start = from_date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        to_end = to_date_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        query_filters &= Q(date__gte=from_start, date__lte=to_end)
    elif from_date_dt:
        # Only from_date provided - filter conversations created from start of this day onwards
        from_start = from_date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        query_filters &= Q(date__gte=from_start)
    elif to_date_dt:
        # Only to_date provided - filter conversations created up to end of this day
        to_end = to_date_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        query_filters &= Q(date__lte=to_end)

    if phone_number:
        query_filters &= Q(phone_number__icontains=phone_number)
    if conversation_status:
        query_filters &= Q(conversation_status=conversation_status)
    if read_by_manager is not None:
        query_filters &= Q(read_by_manager=read_by_manager)
    # change message is not good for filtering conversations
    # if query:
    #     # If a query string is provided, filter conversations that match the query
    #     # This could be a text search or any other specific query logic
    #     query_filters &= Q(phone_number__icontains=query) | Q(message__icontains=query, profile_name__icontains=query)

    if last_message_date_dt:
        # Show conversations with last messages from 00:00 to 23:59 of the selected date
        start_of_day = last_message_date_dt.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_of_day = last_message_date_dt.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        query_filters &= Q(
            last_message_date__gte=start_of_day, last_message_date__lte=end_of_day
        )
    return query_filters


def parse_last_message_date(last_message_date) -> Optional[datetime]:
    """Parse a date string in one of the accepted formats (YYYY-MM-DD or ISO variants); None when invalid."""
    if not last_message_date:
        return None
    try:
        # First try simple date format (e.g., "2024-01-01") - this is the primary expected format
        return datetime.strptime(last_message_date, "%Y-%m-%d")
    except ValueError:
        try:
            # Try ISO format with microseconds and timezone (e.g., "2024-01-01T23:59:59.999Z")
            date_with_tz = last_message_date.replace("Z", "+00:00")
            return datetime.strptime(date_with_tz, "%Y-%m-%dT%H:%M:%S.%f%z").replace(
                tzinfo=None
            )
        except ValueError:
            try:
                # Try ISO format with microseconds (e.g., "2024-01-01T23:59:59.999")
                return datetime.strptime(last_message_date, "%Y-%m-%dT%H:%M:%S.%f")
            except ValueError:
                try:
                    # Try ISO format without microseconds (e.g., "2024-01-01T23:59:59")
                    return datetime.strptime(last_message_date, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    logger.error(f"ERROR   Invalid date format: {last_message_date}")
                    return None


def parse_date(date_str):
    """Parse a date string in one of the accepted formats (YYYY-MM-DD or ISO variants); None when invalid."""
    if not date_str:
        return None
    try:
        # First try simple date format (e.g., "2024-01-01") - this is the primary expected format
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        try:
            # Try ISO format with microseconds and timezone (e.g., "2024-01-01T23:59:59.999Z")
            date_with_tz = date_str.replace("Z", "+00:00")
            return datetime.strptime(date_with_tz, "%Y-%m-%dT%H:%M:%S.%f%z").replace(
                tzinfo=None
            )
        except ValueError:
            try:
                # Try ISO format with microseconds (e.g., "2024-01-01T23:59:59.999")
                return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%f")
            except ValueError:
                try:
                    # Try ISO format without microseconds (e.g., "2024-01-01T23:59:59")
                    return datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    logger.error(f"ERROR   Invalid date format: {date_str}")
                    return None
