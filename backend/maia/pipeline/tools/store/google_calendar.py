"""The seven calendar tools (`st_*`): availability, scheduling, details, rescheduling and cancellation on the tenant's Google Calendar."""

import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

from maia.pipeline.tools.function_decorator import gpt_tool
from maia.pipeline.context import MessageContext
from maia.models.customers import MeetingDocument
from maia.models.google_accounts import GoogleShared
from maia.utils.is_within_opening_hours import is_within_opening_hours
from maia.api.calendar import (
    get_available_time_slots_in_range,
    get_calendar_service,
    is_time_slot_available,
)

logger = logging.getLogger(__name__)

GOOGLE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Fixed behaviour (was a per-tenant variable on the st_schedule_event dynamic tool):
#   False — only the calendar marked ``default=True`` is used (falls back to any calendar).
#   True  — every connected Google calendar of the tenant participates.
INCLUDE_ALL_CALENDARS = False


def _get_scheduling_calendars(tenant_name):
    """Return the GoogleShared calendars used by the scheduling tools.

    Controlled by ``INCLUDE_ALL_CALENDARS``:
      * Off (default) — only the calendar marked ``default=True`` is used.
      * On            — every connected Google calendar of the tenant participates;
                        availability is unioned and each booking is routed to the
                        first calendar that has the requested window free.

    Always falls back to any calendar if neither path returns one, so a tenant
    with a single non-default calendar still works.
    """
    include_all = INCLUDE_ALL_CALENDARS

    if include_all:
        all_cals = list(GoogleShared.objects(tenant_name=tenant_name))
        if all_cals:
            return all_cals

    default = GoogleShared.objects(tenant_name=tenant_name, default=True).first()
    if default:
        return [default]
    any_cal = GoogleShared.objects(tenant_name=tenant_name).first()
    return [any_cal] if any_cal else []


def _build_event_window(service, cal, date: str, time: str, duration_minutes: int):
    calendar_timezone = service.calendarList().get(calendarId=cal.email).execute().get("timeZone")
    logger.info(f"[st_schedule_event] {cal.email} timezone: {calendar_timezone}")
    tz = pytz.timezone(calendar_timezone)
    datetime_obj = tz.localize(datetime.strptime(date + "T" + time, GOOGLE_DATETIME_FORMAT))
    return calendar_timezone, datetime_obj, datetime_obj + timedelta(minutes=duration_minutes)


def _is_calendar_slot_usable(
    service, cal, calendar_timezone, datetime_obj, end_datetime_obj
) -> tuple[bool, str | None]:
    if datetime_obj < datetime.now(pytz.timezone(calendar_timezone)):
        logger.info(f"[st_schedule_event] {cal.email}: REJECTED - time is in the past")
        return False, "past"

    if (
        cal.opening_hours
        and len(cal.opening_hours) > 0
        and (
            not is_within_opening_hours(datetime_obj, cal.opening_hours)
            or not is_within_opening_hours(end_datetime_obj, cal.opening_hours)
        )
    ):
        logger.info(f"[st_schedule_event] {cal.email}: REJECTED - closed")
        return False, "closed"

    is_available = is_time_slot_available(
        service=service,
        calendar_id=cal.email,
        start_time=datetime_obj.isoformat(),
        end_time=end_datetime_obj.isoformat(),
        timezone=calendar_timezone,
        ignore_long_events=getattr(cal, "ignore_long_events", False),
    )
    if not is_available:
        logger.info(f"[st_schedule_event] {cal.email}: REJECTED - busy")
        return False, "busy"

    return True, None


def _build_event_payload(
    request: MessageContext,
    title: str,
    description: str,
    fullname: str,
    email: str,
    phone_number: str,
    calendar_timezone: str,
    datetime_obj: datetime,
    end_datetime_obj: datetime,
):
    event_payload = {
        "summary": title,
        "location": request["sender_id"],
        "description": description
        + "\n"
        + f"שם: {fullname}"
        + "\n"
        + f"אימייל: {email}"
        + "\n"
        + f"טלפון: {phone_number}",
        "start": {"dateTime": datetime_obj.strftime(GOOGLE_DATETIME_FORMAT), "timeZone": calendar_timezone},
        "end": {"dateTime": end_datetime_obj.strftime(GOOGLE_DATETIME_FORMAT), "timeZone": calendar_timezone},
        "conferenceData": {
            "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        },
    }
    if request["platform"] == "email":
        event_payload["attendees"] = [{"email": request["sender_id"]}]
    elif email:
        event_payload["attendees"] = [{"email": email}]
    return event_payload


def _persist_created_meeting(
    request: MessageContext, event: dict, datetime_obj: datetime, end_datetime_obj: datetime
) -> None:
    request["customer"].meetings.append(
        MeetingDocument(
            meeting_id=event["id"],
            meeting_url=event["htmlLink"],
            start_time=datetime_obj.strftime(GOOGLE_DATETIME_FORMAT),
            end_time=end_datetime_obj.strftime(GOOGLE_DATETIME_FORMAT),
            status="confirmed",
        )
    )
    request["customer"].save()


def _create_event_in_first_available_calendar(
    request: MessageContext,
    calendars,
    date: str,
    time: str,
    title: str,
    duration_minutes: int,
    email: str,
    fullname: str,
    description: str,
    phone_number: str,
):
    rejection_reasons = []

    for cal in calendars:
        logger.info(f"\n[st_schedule_event] --- Processing calendar: {cal.email} ---")
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        calendar_timezone, datetime_obj, end_datetime_obj = _build_event_window(
            service, cal, date, time, duration_minutes
        )
        is_usable, reason = _is_calendar_slot_usable(service, cal, calendar_timezone, datetime_obj, end_datetime_obj)
        if not is_usable:
            rejection_reasons.append(reason)
            continue

        event_payload = _build_event_payload(
            request,
            title,
            description,
            fullname,
            email,
            phone_number,
            calendar_timezone,
            datetime_obj,
            end_datetime_obj,
        )
        event = (
            service.events()
            .insert(calendarId=cal.email, body=event_payload, conferenceDataVersion=1, sendUpdates="all")
            .execute()
        )
        _persist_created_meeting(request, event, datetime_obj, end_datetime_obj)
        logger.info(f"[st_schedule_event] {cal.email}: event created {event['id']} — stopping (single-calendar booking)")
        return (cal, event), rejection_reasons

    return None, rejection_reasons


def _format_schedule_rejection(rejection_reasons: list[str]) -> str:
    if rejection_reasons and all(reason == "past" for reason in rejection_reasons):
        return "This time slot is not available because it's in the past"
    if rejection_reasons and all(reason == "closed" for reason in rejection_reasons):
        return "This time slot is not available because the agency is closed"
    return "This time slot is not available"


def _collect_user_future_events(calendars, user_location: str):
    now_google_format = datetime.now(timezone.utc).isoformat() + "Z"
    filtered_pairs = []

    for cal in calendars:
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        events = service.events().list(calendarId=cal.email).execute()
        filtered_pairs.extend(
            (cal, service, event)
            for event in events.get("items", [])
            if event.get("status") == "confirmed"
            and event.get("location") == user_location
            and event.get("start", {}).get("dateTime", now_google_format) > now_google_format
        )

    return filtered_pairs


def _find_customer_event_at(calendars, user_location: str, start_dt: datetime):
    """The customer's own confirmed event that starts exactly at `start_dt`, if any.

    Used to keep booking idempotent: the model tends to call the booking tool again when the
    customer answers "yes" or "thanks" after the appointment was already created.
    """
    for cal, _service, event in _collect_user_future_events(calendars, user_location):
        start = event.get("start", {}).get("dateTime")
        if not start:
            continue
        try:
            if datetime.fromisoformat(start) == start_dt:
                return cal, event
        except ValueError:
            continue
    return None, None


def _mark_customer_meeting_cancelled(customer, event_id: str) -> None:
    for meeting in customer.meetings:
        if meeting.meeting_id == event_id:
            meeting.status = "cancelled"
            customer.save()
            break


def _delete_calendar_event(cal, service, event_to_cancel, customer) -> None:
    service.events().delete(calendarId=cal.email, eventId=event_to_cancel["id"]).execute()
    logger.info(f"[st_cancel_event] Event deleted in {cal.email}: {event_to_cancel.get('htmlLink')}")
    _mark_customer_meeting_cancelled(customer, event_to_cancel["id"])


def _find_calendar_event(calendars, event_id: str):
    for cal in calendars:
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        try:
            event = service.events().get(calendarId=cal.email, eventId=event_id).execute()
            if event:
                logger.info(f"[st_reschedule_event] Event {event_id} found in {cal.email}")
                return cal, service, event
        except Exception as e:
            logger.error(f"[st_reschedule_event] Event not in {cal.email}: {e}")
    return None, None, None


def _build_reschedule_window(target_service, target_cal, new_date: str, new_time: str, duration_minutes: int):
    calendar_timezone = target_service.calendarList().get(calendarId=target_cal.email).execute().get("timeZone")
    tz = pytz.timezone(calendar_timezone)
    datetime_obj = tz.localize(datetime.strptime(new_date + "T" + new_time, GOOGLE_DATETIME_FORMAT))
    return calendar_timezone, datetime_obj, datetime_obj + timedelta(minutes=duration_minutes)


def _validate_reschedule_slot(
    target_service, target_cal, calendar_timezone, datetime_obj, end_datetime_obj
) -> str | None:
    if target_cal.opening_hours:
        starts_open = is_within_opening_hours(datetime_obj, target_cal.opening_hours)
        ends_open = is_within_opening_hours(end_datetime_obj, target_cal.opening_hours)
        if not starts_open or not ends_open:
            return "This time slot is not available because the agency is closed."

    if not is_time_slot_available(
        service=target_service,
        calendar_id=target_cal.email,
        start_time=datetime_obj.isoformat(),
        end_time=end_datetime_obj.isoformat(),
        timezone=calendar_timezone,
        ignore_long_events=getattr(target_cal, "ignore_long_events", False),
    ):
        return "This time slot is not available."
    return None


def _update_customer_rescheduled_meeting(customer, event_id: str, datetime_obj, end_datetime_obj) -> None:
    for meeting in customer.meetings:
        if meeting.meeting_id == event_id:
            meeting.start_time = datetime_obj.strftime(GOOGLE_DATETIME_FORMAT)
            meeting.end_time = end_datetime_obj.strftime(GOOGLE_DATETIME_FORMAT)
            meeting.status = "confirmed"
            customer.save()
            break


def _apply_calendar_reschedule(
    target_service, target_cal, event_id: str, event: dict, calendar_timezone: str, datetime_obj, end_datetime_obj
):
    event["start"] = {"dateTime": datetime_obj.strftime(GOOGLE_DATETIME_FORMAT), "timeZone": calendar_timezone}
    event["end"] = {"dateTime": end_datetime_obj.strftime(GOOGLE_DATETIME_FORMAT), "timeZone": calendar_timezone}
    return target_service.events().update(calendarId=target_cal.email, eventId=event_id, body=event).execute()


@gpt_tool
async def st_schedule_event(
    request: MessageContext,
    date: str,
    time: str,
    title: str,
    duration_minutes: int = 30,
    email: Optional[str] = None,
    fullname: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Schedule an event for the user on Google Calendar.

    date: Date of the event in ISO format (YYYY-MM-DD)
    time: Time of the event in HH:mm:ss format
    title: Title for the event
    duration_minutes: Duration of the event in minutes
    email: Email address of the user
    fullname: Full name of the user
    description: Description of the event
    """
    fullname = fullname or ""
    email = email or ""
    description = description or ""
    phone_number = request["sender_id"]

    logger.info(f"\n{'#' * 60}")
    logger.info("[st_schedule_event] CALLED")
    logger.info(f"[st_schedule_event] tenant_name: {request['tenant_name']}")
    logger.info(f"[st_schedule_event] chosen_date: {date}")
    logger.info(f"[st_schedule_event] chosen_time: {time}")
    logger.info(f"[st_schedule_event] duration_minutes: {duration_minutes}")
    logger.info(f"[st_schedule_event] fullname: {fullname}")
    logger.info(f"[st_schedule_event] email: {email}")

    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_schedule_event] No calendar for create event")
        return "We can't create an event for the user"

    logger.info(f"[st_schedule_event] {len(calendars)} scheduling calendar(s): {[c.email for c in calendars]}")

    # Idempotency: never create the same appointment twice for the same customer.
    for cal in calendars:
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        _, requested_start, _ = _build_event_window(service, cal, date, time, duration_minutes)
        _, existing_event = _find_customer_event_at([cal], phone_number, requested_start)
        if existing_event:
            logger.info(f"[st_schedule_event] {cal.email}: appointment {existing_event['id']} already exists, not created again")
            logger.info(f"{'#' * 60}\n")
            return (
                f"This appointment already exists and is confirmed: '{existing_event.get('summary')}' at {date}T{time} "
                f"(event_id {existing_event['id']}). Do not create it again; just confirm it to the customer."
            )

    datetime_str_user = date + "T" + time
    created_event, rejection_reasons = _create_event_in_first_available_calendar(
        request=request,
        calendars=calendars,
        date=date,
        time=time,
        title=title,
        duration_minutes=duration_minutes,
        email=email,
        fullname=fullname,
        description=description,
        phone_number=phone_number,
    )

    if not created_event:
        logger.info(f"[st_schedule_event] No event created across {len(calendars)} calendar(s). Reasons: {rejection_reasons}")
        logger.info(f"{'#' * 60}\n")
        return _format_schedule_rejection(rejection_reasons)

    logger.info(f"[st_schedule_event] event created in {created_event[0].email}")
    logger.info(f"{'#' * 60}\n")
    return (
        f"Appointment created and confirmed: '{title}' at {datetime_str_user} "
        f"(event_id {created_event[1]['id']}). Do not create it again."
    )


# --------------------------------------------------------


@gpt_tool
async def st_cancel_event(request: MessageContext, event_id: Optional[str] = None) -> str:
    """
    Cancels a scheduled event for the user.

    event_id: The unique identifier of the event to be canceled
    """
    # remove event for the user we not need to delete reminder,
    # because if the use send message, is automatically deleted
    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_cancel_event] No calendar for cancel event")
        return "We can't cancel the event for the user"

    # list of (calendar, service, event) for every confirmed future event at the user's location
    all_filtered_pairs = _collect_user_future_events(calendars, request["sender_id"])

    all_filtered_events = [ev for _, _, ev in all_filtered_pairs]
    is_cancelled = False

    # In case the user chose a specific event from the list
    if event_id:
        target = next(((c, s, e) for c, s, e in all_filtered_pairs if e["id"] == event_id), None)
        if not target:
            logger.info("[st_cancel_event] This event doesn't belong to the user.")
            return "This event doesn't belong to the user. Let him choose from the list."
        cal, service, event_to_cancel = target
        _delete_calendar_event(cal, service, event_to_cancel, request["customer"])
        is_cancelled = True

    else:  # In case the user asks to cancel event but didn't provide id
        if len(all_filtered_pairs) > 1:
            logger.info(" ".join(str(part) for part in ("[st_cancel_event] The user have multiple events: ", all_filtered_events)))
            return "The user have multiple events, let him choose the one he wants to cancel: " + str(
                all_filtered_events
            )
        if len(all_filtered_pairs) == 1:
            cal, service, event_to_cancel = all_filtered_pairs[0]
            _delete_calendar_event(cal, service, event_to_cancel, request["customer"])
            is_cancelled = True

    if len(all_filtered_pairs) == 0:
        logger.info("[st_cancel_event] We don't have any event for the user to cancel")
        return "We don't have any event for the user to cancel"

    if is_cancelled:
        logger.info("[st_cancel_event] Ok, we have canceled the event for the user")
        return "Ok, we have canceled the event for the user"


# --------------------------------------------------------


@gpt_tool
async def st_get_event_details(request: MessageContext, event_id: Optional[str] = None) -> str:
    """
    Retrieve details about a scheduled event.

    event_id: The unique identifier of the event to retrieve details for
    """
    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_get_event_details] No calendar for events")
        return "There's no events for the user"

    now_google_format = datetime.now(timezone.utc).isoformat() + "Z"
    filtered_events = []

    for cal in calendars:
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        events = service.events().list(calendarId=cal.email, timeMin=datetime.now().isoformat() + "Z").execute()
        filtered_events.extend(
            [
                event
                for event in events.get("items", [])
                if event.get("status") == "confirmed"
                and event.get("location") == request["sender_id"]
                and event.get("start", {}).get("dateTime", now_google_format) > now_google_format
            ]
        )

    # In case the user chose a specific event from the list to get more details about
    if event_id:
        for event in filtered_events:
            if event_id == event["id"]:
                logger.info(" ".join(str(part) for part in ("The event details: ", event)))
                return "The event details: " + str(event)
    if len(filtered_events) > 1:
        logger.info(" ".join(str(part) for part in ("The user have multiple events.", filtered_events)))
        return "The user have multiple events: " + str(filtered_events)
    if len(filtered_events) == 0:
        logger.info("The user have no events.")
        return "The user have no events."
    else:
        logger.info(" ".join(str(part) for part in ("The event details: ", filtered_events)))
        return "The event details: " + str(filtered_events)


# --------------------------------------------------------


@gpt_tool
async def st_check_availability_by_date_and_time(
    request: MessageContext, date: str, time: str, duration_minutes: int = 30
) -> str:
    """
    Checks availability based on a specific date and time provided by the user.

    date: The specific date for which to check availability, formatted as YYYY-MM-DD
    time: The specific time for which to check availability, formatted as HH:MM:SS
    duration_minutes: Duration of the event in minutes
    """
    # Validate date format - must be YYYY-MM-DD
    date_pattern = r"^\d{4}-\d{2}-\d{2}$"
    time_pattern = r"^\d{2}:\d{2}:\d{2}$"

    if not date or not re.match(date_pattern, str(date)):
        logger.error(f"[ERROR st_check_availability_by_date_and_time] Invalid date format: {date}")
        return f"Invalid date format: '{date}'. Please provide date in YYYY-MM-DD format (e.g., 2026-01-20)."
    if not time or not re.match(time_pattern, str(time)):
        logger.error(f"[ERROR st_check_availability_by_date_and_time] Invalid time format: {time}")
        return f"Invalid time format: '{time}'. Please provide time in HH:MM:SS format (e.g., 14:30:00)."

    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_check_availability_by_date_and_time] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    # Available if at least ONE calendar has the slot free — that calendar will receive the event
    for cal in calendars:
        service = get_calendar_service(cal.access_token, cal.refresh_token)
        calendar_timezone = service.calendarList().get(calendarId=cal.email).execute().get("timeZone")

        tz = pytz.timezone(calendar_timezone)
        datetime_obj = tz.localize(datetime.strptime(date + "T" + time, GOOGLE_DATETIME_FORMAT))
        end_datetime_obj = datetime_obj + timedelta(minutes=duration_minutes)

        if (
            cal.opening_hours
            and len(cal.opening_hours) > 0
            and (
                not is_within_opening_hours(datetime_obj, cal.opening_hours)
                or not is_within_opening_hours(end_datetime_obj, cal.opening_hours)
            )
        ):
            logger.info(f"[st_check_availability_by_date_and_time] {cal.email}: closed at this time")
            continue

        if is_time_slot_available(
            service=service,
            calendar_id=cal.email,
            start_time=datetime_obj.isoformat(),
            end_time=end_datetime_obj.isoformat(),
            timezone=calendar_timezone,
            ignore_long_events=getattr(cal, "ignore_long_events", False),
        ):
            logger.info(f"[st_check_availability_by_date_and_time] {cal.email}: AVAILABLE")
            return "This time slot is available"
        _, own_event = _find_customer_event_at([cal], request["sender_id"], datetime_obj)
        if own_event:
            logger.info(f"[st_check_availability_by_date_and_time] {cal.email}: taken by the customer's own {own_event['id']}")
            return (
                f"This time is already booked for this customer: their own appointment '{own_event.get('summary')}' "
                f"(event_id {own_event['id']}). It is confirmed; do not book it again."
            )
        logger.info(f"[st_check_availability_by_date_and_time] {cal.email}: busy")

    logger.info("[st_check_availability_by_date_and_time] No calendar available for this slot")
    return "This time slot is not available"


# --------------------------------------------------------


@gpt_tool
async def st_get_available_times_by_range(
    request: MessageContext,
    date_from: str,
    date_to: str,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
) -> str:
    """
    Gets the available times in the calendar based on provided dates or relative time indicators like 'next week'. If 'next week' is given, it will automatically use the start of the next week (Sunday) and end of the next week (Saturday) based on Israel's calendar system.

    date_from: The start date from which to check availability, in ISO 8601 format (YYYY-MM-DD) or as a relative week indicator (e.g., 'next week')
    date_to: The end date until which to check availability, in ISO 8601 format (YYYY-MM-DD) or as a relative week indicator (e.g., 'next week')
    time_from: The start time of the start date from which to check availability, in HH:MM:SS format
    time_to: The end time of the end date until which to check availability, in HH:MM:SS format
    """
    logger.info(f"[DEBUG st_get_available_times_by_range] Received arguments: date_from={date_from}, date_to={date_to}, time_from={time_from}, time_to={time_to}")

    # Validate date format - must be YYYY-MM-DD
    date_pattern = r"^\d{4}-\d{2}-\d{2}$"
    if not date_from or not re.match(date_pattern, str(date_from)):
        logger.error(f"[ERROR st_get_available_times_by_range] Invalid date_from format: {date_from}")
        return f"Invalid date format for date_from: '{date_from}'. Please provide dates in YYYY-MM-DD format (e.g., 2026-01-20)."
    if not date_to or not re.match(date_pattern, str(date_to)):
        logger.error(f"[ERROR st_get_available_times_by_range] Invalid date_to format: {date_to}")
        return f"Invalid date format for date_to: '{date_to}'. Please provide dates in YYYY-MM-DD format (e.g., 2026-01-20)."

    if not time_from:
        time_from = "00:00:00"
    if not time_to:
        time_to = "23:59:59"

    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_get_available_times_by_range] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    logger.info(f"[st_get_available_times_by_range] {len(calendars)} calendar(s): {[c.email for c in calendars]}")

    filtered_available_slots = _get_availability_union(calendars, date_from, time_from, date_to, time_to)

    logger.info(f"[st_get_available_times_by_range] union returned {len(filtered_available_slots)} slots")

    filtered_available_slots = _simplify_slot_structure(filtered_available_slots)

    logger.info(f"[st_get_available_times_by_range] After simplify: {len(filtered_available_slots)} slots")
    logger.info(f"[st_get_available_times_by_range] First 20 slots: {filtered_available_slots[:20]}")

    if len(filtered_available_slots) > 0:
        logger.info(f"[st_get_available_times_by_range] Returning {len(filtered_available_slots)} available times")
        return "Available times: " + str(filtered_available_slots)
    if len(filtered_available_slots) == 0:
        logger.info("[st_get_available_times_by_range] No available times.")
        return "No available times."


# --------------------------------------------------------


@gpt_tool
async def st_get_available_event_times(request: MessageContext) -> str:
    """
    Get available times from the calendar for events when no dates/times/indicators are provided by the user.
    """
    logger.info(f"\n{'#' * 60}")
    logger.info("[st_get_available_event_times] CALLED")
    logger.info(f"[st_get_available_event_times] tenant_name: {request['tenant_name']}")

    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_get_available_event_times] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    logger.info(f"[st_get_available_event_times] {len(calendars)} calendar(s): {[c.email for c in calendars]}")

    # Use the first calendar's timezone to compute "today" — calendars usually share a tz
    ref_cal = calendars[0]
    ref_service = get_calendar_service(ref_cal.access_token, ref_cal.refresh_token)
    calendar_timezone = ref_service.calendarList().get(calendarId=ref_cal.email).execute().get("timeZone")
    logger.info(f"[st_get_available_event_times] reference timezone (from {ref_cal.email}): {calendar_timezone}")

    tz = pytz.timezone(calendar_timezone)
    today = datetime.now(tz).date()
    logger.info(f"[st_get_available_event_times] today (in calendar timezone): {today}")

    date_from = today + timedelta(days=1)
    date_to = date_from + timedelta(days=6)  # 6 more days after tomorrow

    date_from = date_from.strftime("%Y-%m-%d")
    date_to = date_to.strftime("%Y-%m-%d")
    time_from = "00:00:00"
    time_to = "23:59:59"

    logger.info("[st_get_available_event_times] Calling _get_availability_union with:")
    logger.info(f"[st_get_available_event_times]   date_from: {date_from}")
    logger.info(f"[st_get_available_event_times]   date_to: {date_to}")

    filtered_available_slots = _get_availability_union(calendars, date_from, time_from, date_to, time_to)

    logger.info(f"[st_get_available_event_times] union returned {len(filtered_available_slots)} slots")

    filtered_available_slots = _simplify_slot_structure(filtered_available_slots)

    logger.info(f"[st_get_available_event_times] After simplify: {len(filtered_available_slots)} slots")
    logger.info(f"[st_get_available_event_times] First 20 slots: {filtered_available_slots[:20]}")

    if len(filtered_available_slots) == 0:
        logger.info("[st_get_available_event_times] No available times.")
        logger.info(f"{'#' * 60}\n")
        return "No available times."

    # For phone calls, return only 2-3 suggestions from different days
    if request.get("platform") == "telephone":
        filtered_available_slots = _pick_suggested_slots(filtered_available_slots)
        logger.info(f"[st_get_available_event_times] Telephone mode - returning {len(filtered_available_slots)} suggestions")
        logger.info(f"{'#' * 60}\n")
        return "Suggested available times: " + str(filtered_available_slots)

    logger.info(f"[st_get_available_event_times] Returning {len(filtered_available_slots)} available times")
    logger.info(f"{'#' * 60}\n")
    return "Available times: " + str(filtered_available_slots)


# --------------------------------------------------------


@gpt_tool
async def st_reschedule_event(
    request: MessageContext, new_date: str, new_time: str, event_id: Optional[str] = None, duration_minutes: int = 30
) -> str:
    """
    Reschedule an existing scheduled event.

    new_date: The new date for the event in ISO 8601 format (YYYY-MM-DD)
    new_time: The new time for the event, must be in HH:mm:ss format
    event_id: The unique identifier of the event to be rescheduled. This can be the ID of the event selected by the user from their list of events, or the ID of their only event.
    duration_minutes: Duration of the event in minutes
    """
    if not event_id:
        return "Missing event ID for rescheduling. Get the events of the user and let him choose which one he wants to reschedule."

    calendars = _get_scheduling_calendars(request["tenant_name"])
    if not calendars:
        logger.info("[st_reschedule_event] No calendar for rescheduling")
        return "We can't reschedule the event because the calendar doesn't exist."

    target_cal, target_service, event = _find_calendar_event(calendars, event_id)
    if not target_cal or not event:
        logger.warning(f"[st_reschedule_event] Event {event_id} not found in any calendar")
        return "Event not found. Get the events of the user and let him choose which one he wants to reschedule."

    calendar_timezone, datetime_obj, end_datetime_obj = _build_reschedule_window(
        target_service, target_cal, new_date, new_time, duration_minutes
    )
    if unavailable_message := _validate_reschedule_slot(
        target_service, target_cal, calendar_timezone, datetime_obj, end_datetime_obj
    ):
        return unavailable_message

    try:
        updated_event = _apply_calendar_reschedule(
            target_service, target_cal, event_id, event, calendar_timezone, datetime_obj, end_datetime_obj
        )
        _update_customer_rescheduled_meeting(request["customer"], event_id, datetime_obj, end_datetime_obj)
        logger.info(" ".join(str(part) for part in ("Event rescheduled:", updated_event)))
        return f"Ok, I have rescheduled your event to {new_date} at {new_time}."

    except Exception as e:
        logger.error(" ".join(str(part) for part in ("Error during rescheduling:", str(e))))
        return "Something went wrong while trying to reschedule the event."


# --------------------------------------------------------
# Helper functions (not decorated - used internally by the tools above)
# --------------------------------------------------------


def _pick_suggested_slots(simplified_slots):
    """Pick 2-3 suggested slots from different days (for phone calls)"""
    # Group by day
    slots_by_day = {}
    for slot in simplified_slots:
        day = slot["date"]
        if day not in slots_by_day:
            slots_by_day[day] = []
        slots_by_day[day].append(slot)

    # Pick up to 3 different days
    available_days = sorted(slots_by_day.keys())
    selected_days = available_days[:3] if len(available_days) <= 3 else random.sample(available_days, 3)
    selected_days.sort()

    # From each day, pick a mid-range slot (not the earliest/latest)
    suggestions = []
    for day in selected_days:
        day_slots = slots_by_day[day]
        n = len(day_slots)
        if n >= 3:
            mid_start = n // 3
            mid_end = 2 * n // 3
            slot = random.choice(day_slots[mid_start:mid_end])
        else:
            slot = day_slots[0]
        suggestions.append(slot)

    return suggestions


def _get_availability(calendar, date_from, time_from, date_to, time_to):
    """Get available times for range of dates and times for a single calendar."""
    logger.info(f"\n{'=' * 60}")
    logger.info("[_get_availability] CALLED")
    logger.info(f"[_get_availability] calendar.email: {calendar.email}")
    logger.info(f"[_get_availability] calendar.ignore_long_events: {getattr(calendar, 'ignore_long_events', 'NOT SET')}")
    logger.info(f"[_get_availability] date_from: {date_from}")
    logger.info(f"[_get_availability] time_from: {time_from}")
    logger.info(f"[_get_availability] date_to: {date_to}")
    logger.info(f"[_get_availability] time_to: {time_to}")

    service = get_calendar_service(calendar.access_token, calendar.refresh_token)

    # get the calendar's timezone
    calendar_timezone = service.calendarList().get(calendarId=calendar.email).execute().get("timeZone")

    logger.info(f"[_get_availability] calendar_timezone from API: {calendar_timezone}")

    # create end datetime
    date_from_str = date_from + "T" + time_from
    date_from_obj = datetime.strptime(date_from_str, GOOGLE_DATETIME_FORMAT)
    date_to_str = date_to + "T" + time_to
    date_to_obj = datetime.strptime(date_to_str, GOOGLE_DATETIME_FORMAT)

    logger.info(f"[_get_availability] date_from_str: {date_from_str}")
    logger.info(f"[_get_availability] date_to_str: {date_to_str}")

    # FIX: Use localize() instead of astimezone() for naive datetimes
    # astimezone() assumes the naive datetime is in system local timezone, which is wrong
    # We want to treat the datetime as being in the calendar's timezone
    tz = pytz.timezone(calendar_timezone)
    date_from_obj = tz.localize(date_from_obj)
    date_to_obj = tz.localize(date_to_obj)

    logger.info(f"[_get_availability] date_from_obj (localized): {date_from_obj}")
    logger.info(f"[_get_availability] date_to_obj (localized): {date_to_obj}")

    logger.info("[_get_availability] Calling get_available_time_slots_in_range...")

    # Get available time slots in range of dates
    available_slots = get_available_time_slots_in_range(
        service=service,
        calendar_id=calendar.email,
        start_date=date_from_obj.isoformat(),
        end_date=date_to_obj.isoformat(),
        timezone=calendar_timezone,
        ignore_long_events=getattr(calendar, "ignore_long_events", False),
    )

    logger.info(f"[_get_availability] get_available_time_slots_in_range returned {len(available_slots)} slots")

    filtered_available_slots = []
    for start_datetime, end_datetime in available_slots:
        # Convert the start and end datetime to the correct timezone
        start_datetime = start_datetime.astimezone(pytz.timezone(calendar_timezone))
        end_datetime = end_datetime.astimezone(pytz.timezone(calendar_timezone))

        # Check if the slot is within the opening hours
        if calendar.opening_hours and len(calendar.opening_hours) > 0:
            # Check if the start and end datetime are within the opening hours
            is_open = is_within_opening_hours(start_datetime, calendar.opening_hours)
            end_is_open = is_within_opening_hours(end_datetime, calendar.opening_hours)

            # If either start or end time is outside the opening hours, skip this slot
            if not is_open or not end_is_open:
                continue

            if is_open and end_is_open:
                # If the slot is within the opening hours, append it to the result list
                filtered_available_slots.append((start_datetime, end_datetime))

    logger.info(f"[_get_availability] After opening hours filter: {len(filtered_available_slots)} slots")
    logger.info(f"{'=' * 60}\n")

    return filtered_available_slots


def _get_availability_union(calendars, date_from, time_from, date_to, time_to):
    """Union of available slots across multiple calendars.

    A slot is included if at least one calendar has it free. This matches the
    scheduling logic — the event will be created in whichever calendar(s) are
    available at that moment.

    Slots are deduplicated by their UTC instant so calendars in different
    timezones still merge correctly. The result is sorted by start time.
    """
    seen = set()
    union = []
    for cal in calendars:
        slots = _get_availability(cal, date_from, time_from, date_to, time_to)
        for start, end in slots:
            key = (start.astimezone(pytz.UTC).isoformat(), end.astimezone(pytz.UTC).isoformat())
            if key in seen:
                continue
            seen.add(key)
            union.append((start, end))
    union.sort(key=lambda pair: pair[0])
    return union


def _simplify_slot_structure(slot_list):
    return [
        {"date": start.strftime("%d-%m-%Y"), "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
        for start, end in slot_list
    ]
