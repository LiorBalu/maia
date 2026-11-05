"""The seven calendar tools (`st_*`): availability, scheduling, details, rescheduling and cancellation on the tenant's Google Calendar."""

import logging
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz

from maia.pipeline.tools.function_decorator import llm_tool
from maia.pipeline.context import MessageContext
from maia.models.customers import MeetingDocument
from maia.models.google_accounts import GoogleShared
from maia.utils.is_within_opening_hours import is_datetime_within_opening_hours
from maia.api.calendar import (
    find_free_slots_in_range,
    build_calendar_client,
    is_slot_free_in_calendar,
)

logger = logging.getLogger(__name__)

NAIVE_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Fixed behaviour (was a per-tenant variable on the st_book_appointment dynamic tool):
#   False — only the calendar marked ``default=True`` is used (falls back to any calendar).
#   True  — every connected Google calendar of the tenant participates.
SHOULD_USE_ALL_CONNECTED_CALENDARS = False


def _resolve_scheduling_calendars(tenant_name):
    """Return the GoogleShared calendars used by the scheduling tools.

    Controlled by ``SHOULD_USE_ALL_CONNECTED_CALENDARS``:
      * Off (default) — only the calendar marked ``default=True`` is used.
      * On            — every connected Google calendar of the tenant participates;
                        availability is unioned and each booking is routed to the
                        first calendar that has the requested window free.

    Always falls back to any calendar if neither path returns one, so a tenant
    with a single non-default calendar still works.
    """
    should_use_all_calendars = SHOULD_USE_ALL_CONNECTED_CALENDARS

    if should_use_all_calendars:
        connected_calendar_accounts = list(GoogleShared.objects(tenant_name=tenant_name))
        if connected_calendar_accounts:
            return connected_calendar_accounts

    default_calendar_account = GoogleShared.objects(tenant_name=tenant_name, default=True).first()
    if default_calendar_account:
        return [default_calendar_account]
    fallback_calendar_account = GoogleShared.objects(tenant_name=tenant_name).first()
    return [fallback_calendar_account] if fallback_calendar_account else []


def _localize_requested_slot(calendar_client, calendar_account, requested_date: str, requested_time: str, event_duration_minutes: int):
    """Localize the requested date + time in the calendar's timezone; returns (timezone, start, end)."""
    calendar_timezone_name = calendar_client.calendarList().get(calendarId=calendar_account.email).execute().get("timeZone")
    logger.info(f"[st_book_appointment] {calendar_account.email} timezone: {calendar_timezone_name}")
    calendar_timezone = pytz.timezone(calendar_timezone_name)
    slot_start = calendar_timezone.localize(datetime.strptime(requested_date + "T" + requested_time, NAIVE_DATETIME_FORMAT))
    return calendar_timezone_name, slot_start, slot_start + timedelta(minutes=event_duration_minutes)


def _check_slot_is_bookable(
    calendar_client, calendar_account, calendar_timezone_name, slot_start, slot_end
) -> tuple[bool, str | None]:
    """Reject a slot that is in the past, outside the opening hours or busy; returns (usable, reason)."""
    if slot_start < datetime.now(pytz.timezone(calendar_timezone_name)):
        logger.info(f"[st_book_appointment] {calendar_account.email}: REJECTED - time is in the past")
        return False, "past"

    if (
        calendar_account.opening_hours
        and len(calendar_account.opening_hours) > 0
        and (
            not is_datetime_within_opening_hours(slot_start, calendar_account.opening_hours)
            or not is_datetime_within_opening_hours(slot_end, calendar_account.opening_hours)
        )
    ):
        logger.info(f"[st_book_appointment] {calendar_account.email}: REJECTED - closed")
        return False, "closed"

    is_slot_free = is_slot_free_in_calendar(
        calendar_client=calendar_client,
        google_calendar_id=calendar_account.email,
        slot_start_iso=slot_start.isoformat(),
        slot_end_iso=slot_end.isoformat(),
        timezone_name=calendar_timezone_name,
        should_ignore_long_events=getattr(calendar_account, "ignore_long_events", False),
    )
    if not is_slot_free:
        logger.info(f"[st_book_appointment] {calendar_account.email}: REJECTED - busy")
        return False, "busy"

    return True, None


def _compose_calendar_event_body(
    message_context: MessageContext,
    event_title: str,
    event_description: str,
    customer_full_name: str,
    customer_email: str,
    customer_sender_id: str,
    calendar_timezone_name: str,
    slot_start: datetime,
    slot_end: datetime,
):
    """The Google Calendar event body: title, the customer's number as location, name / email / phone in the
    description, a Meet link, and the customer as attendee when an email is known.
    """
    event_body = {
        "summary": event_title,
        "location": message_context["sender_id"],
        "description": event_description
        + "\n"
        + f"שם: {customer_full_name}"
        + "\n"
        + f"אימייל: {customer_email}"
        + "\n"
        + f"טלפון: {customer_sender_id}",
        "start": {"dateTime": slot_start.strftime(NAIVE_DATETIME_FORMAT), "timeZone": calendar_timezone_name},
        "end": {"dateTime": slot_end.strftime(NAIVE_DATETIME_FORMAT), "timeZone": calendar_timezone_name},
        "conferenceData": {
            "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        },
    }
    if message_context["platform"] == "email":
        event_body["attendees"] = [{"email": message_context["sender_id"]}]
    elif customer_email:
        event_body["attendees"] = [{"email": customer_email}]
    return event_body


def _record_meeting_on_customer(
    message_context: MessageContext, calendar_event: dict, slot_start: datetime, slot_end: datetime
) -> None:
    """Record the booked event on the customer (meetings list) and save."""
    message_context["customer"].meetings.append(
        MeetingDocument(
            meeting_id=calendar_event["id"],
            meeting_url=calendar_event["htmlLink"],
            start_time=slot_start.strftime(NAIVE_DATETIME_FORMAT),
            end_time=slot_end.strftime(NAIVE_DATETIME_FORMAT),
            status="confirmed",
        )
    )
    message_context["customer"].save()


def _insert_event_and_record_meeting(
    message_context: MessageContext,
    calendar_client,
    calendar_account,
    event_body: dict,
    slot_start: datetime,
    slot_end: datetime,
) -> dict:
    """Create the event in this calendar (Meet link, invitations sent), record it on the customer and return it."""
    calendar_event = (
        calendar_client.events()
        .insert(calendarId=calendar_account.email, body=event_body, conferenceDataVersion=1, sendUpdates="all")
        .execute()
    )
    _record_meeting_on_customer(message_context, calendar_event, slot_start, slot_end)
    logger.info(f"[st_book_appointment] {calendar_account.email}: event created {calendar_event['id']} — stopping (single-calendar booking)")
    return calendar_event


def _book_event_in_first_free_calendar(
    message_context: MessageContext,
    calendar_accounts,
    requested_date: str,
    requested_time: str,
    event_title: str,
    event_duration_minutes: int,
    customer_email: str,
    customer_full_name: str,
    event_description: str,
    customer_sender_id: str,
):
    """Try the calendars in order and create the event in the first one where the slot is usable; returns ((calendar,
    event) or None, rejection reasons).
    """
    slot_rejection_reasons = []

    for calendar_account in calendar_accounts:
        logger.info(f"\n[st_book_appointment] --- Processing calendar: {calendar_account.email} ---")
        calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
        calendar_timezone_name, slot_start, slot_end = _localize_requested_slot(
            calendar_client, calendar_account, requested_date, requested_time, event_duration_minutes
        )
        is_bookable, rejection_reason = _check_slot_is_bookable(
            calendar_client, calendar_account, calendar_timezone_name, slot_start, slot_end
        )
        if not is_bookable:
            slot_rejection_reasons.append(rejection_reason)
            continue

        event_body = _compose_calendar_event_body(
            message_context,
            event_title,
            event_description,
            customer_full_name,
            customer_email,
            customer_sender_id,
            calendar_timezone_name,
            slot_start,
            slot_end,
        )
        calendar_event = _insert_event_and_record_meeting(
            message_context, calendar_client, calendar_account, event_body, slot_start, slot_end
        )
        return (calendar_account, calendar_event), slot_rejection_reasons

    return None, slot_rejection_reasons


def _compose_booking_rejection_message(slot_rejection_reasons: list[str]) -> str:
    """Text for the model when no calendar could take the slot (past / closed / busy)."""
    if slot_rejection_reasons and all(reason == "past" for reason in slot_rejection_reasons):
        return "This time slot is not available because it's in the past"
    if slot_rejection_reasons and all(reason == "closed" for reason in slot_rejection_reasons):
        return "This time slot is not available because the agency is closed"
    return "This time slot is not available"


def _collect_customer_upcoming_events(calendar_accounts, customer_sender_id: str):
    """(calendar, service, event) of every confirmed future event whose location is the customer's number."""
    current_time_iso = datetime.now(timezone.utc).isoformat() + "Z"
    customer_event_triples = []

    for calendar_account in calendar_accounts:
        calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
        events_response = calendar_client.events().list(calendarId=calendar_account.email).execute()
        customer_event_triples.extend(
            (calendar_account, calendar_client, calendar_event)
            for calendar_event in events_response.get("items", [])
            if calendar_event.get("status") == "confirmed"
            and calendar_event.get("location") == customer_sender_id
            and calendar_event.get("start", {}).get("dateTime", current_time_iso) > current_time_iso
        )

    return customer_event_triples


def _find_customer_event_starting_at(calendar_accounts, customer_sender_id: str, slot_start: datetime):
    """The customer's own confirmed event that starts exactly at `slot_start`, if any.

    Used to keep booking idempotent: the model tends to call the booking tool again when the
    customer answers "yes" or "thanks" after the appointment was already created.
    """
    for calendar_account, _calendar_client, calendar_event in _collect_customer_upcoming_events(
        calendar_accounts, customer_sender_id
    ):
        event_start_iso = calendar_event.get("start", {}).get("dateTime")
        if not event_start_iso:
            continue
        try:
            if datetime.fromisoformat(event_start_iso) == slot_start:
                return calendar_account, calendar_event
        except ValueError:
            continue
    return None, None


def _record_meeting_cancellation(customer, calendar_event_id: str) -> None:
    """Set the status of the customer's stored meeting to cancelled."""
    for meeting in customer.meetings:
        if meeting.meeting_id == calendar_event_id:
            meeting.status = "cancelled"
            customer.save()
            break


def _delete_event_and_record_cancellation(calendar_account, calendar_client, target_event, customer) -> None:
    """Delete the event from Google Calendar and mark the customer's meeting cancelled."""
    calendar_client.events().delete(calendarId=calendar_account.email, eventId=target_event["id"]).execute()
    logger.info(f"[st_cancel_appointment] Event deleted in {calendar_account.email}: {target_event.get('htmlLink')}")
    _record_meeting_cancellation(customer, target_event["id"])


def _find_event_across_calendars(calendar_accounts, calendar_event_id: str):
    """Look an event id up in each calendar; returns (calendar, service, event), or Nones."""
    for calendar_account in calendar_accounts:
        calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
        try:
            calendar_event = calendar_client.events().get(calendarId=calendar_account.email, eventId=calendar_event_id).execute()
            if calendar_event:
                logger.info(f"[st_reschedule_appointment] Event {calendar_event_id} found in {calendar_account.email}")
                return calendar_account, calendar_client, calendar_event
        except Exception as error:
            logger.error(f"[st_reschedule_appointment] Event not in {calendar_account.email}: {error}")
    return None, None, None


def _localize_rescheduled_slot(target_calendar_client, target_calendar_account, rescheduled_date: str, rescheduled_time: str, event_duration_minutes: int):
    """Localize the new date + time in the target calendar's timezone; returns (timezone, start, end)."""
    calendar_timezone_name = target_calendar_client.calendarList().get(calendarId=target_calendar_account.email).execute().get("timeZone")
    calendar_timezone = pytz.timezone(calendar_timezone_name)
    slot_start = calendar_timezone.localize(datetime.strptime(rescheduled_date + "T" + rescheduled_time, NAIVE_DATETIME_FORMAT))
    return calendar_timezone_name, slot_start, slot_start + timedelta(minutes=event_duration_minutes)


def _find_rescheduled_slot_conflict(
    target_calendar_client, target_calendar_account, calendar_timezone_name, slot_start, slot_end
) -> str | None:
    """Message for the model when the new slot is outside the opening hours or busy; None when it is free."""
    if target_calendar_account.opening_hours:
        is_start_within_hours = is_datetime_within_opening_hours(slot_start, target_calendar_account.opening_hours)
        is_end_within_hours = is_datetime_within_opening_hours(slot_end, target_calendar_account.opening_hours)
        if not is_start_within_hours or not is_end_within_hours:
            return "This time slot is not available because the agency is closed."

    if not is_slot_free_in_calendar(
        calendar_client=target_calendar_client,
        google_calendar_id=target_calendar_account.email,
        slot_start_iso=slot_start.isoformat(),
        slot_end_iso=slot_end.isoformat(),
        timezone_name=calendar_timezone_name,
        should_ignore_long_events=getattr(target_calendar_account, "ignore_long_events", False),
    ):
        return "This time slot is not available."
    return None


def _record_meeting_new_times(customer, calendar_event_id: str, slot_start, slot_end) -> None:
    """Update the times of the customer's stored meeting after a reschedule."""
    for meeting in customer.meetings:
        if meeting.meeting_id == calendar_event_id:
            meeting.start_time = slot_start.strftime(NAIVE_DATETIME_FORMAT)
            meeting.end_time = slot_end.strftime(NAIVE_DATETIME_FORMAT)
            meeting.status = "confirmed"
            customer.save()
            break


def _move_event_to_slot(
    target_calendar_client, target_calendar_account, calendar_event_id: str, calendar_event: dict, calendar_timezone_name: str, slot_start, slot_end
):
    """Write the new start / end on the Google Calendar event."""
    calendar_event["start"] = {"dateTime": slot_start.strftime(NAIVE_DATETIME_FORMAT), "timeZone": calendar_timezone_name}
    calendar_event["end"] = {"dateTime": slot_end.strftime(NAIVE_DATETIME_FORMAT), "timeZone": calendar_timezone_name}
    return target_calendar_client.events().update(calendarId=target_calendar_account.email, eventId=calendar_event_id, body=calendar_event).execute()


def _log_booking_call(
    message_context: MessageContext,
    requested_date: str,
    requested_time: str,
    event_duration_minutes: int,
    customer_full_name: str,
    customer_email: str,
) -> None:
    """The entry log lines of st_book_appointment: the tenant and the requested booking."""
    logger.info(f"\n{'#' * 60}")
    logger.info("[st_book_appointment] CALLED")
    logger.info(f"[st_book_appointment] tenant_name: {message_context['tenant_name']}")
    logger.info(f"[st_book_appointment] chosen_date: {requested_date}")
    logger.info(f"[st_book_appointment] chosen_time: {requested_time}")
    logger.info(f"[st_book_appointment] duration_minutes: {event_duration_minutes}")
    logger.info(f"[st_book_appointment] fullname: {customer_full_name}")
    logger.info(f"[st_book_appointment] email: {customer_email}")


def _find_duplicate_booking(
    calendar_accounts, customer_sender_id: str, requested_date: str, requested_time: str, event_duration_minutes: int
):
    """The customer's confirmed event that already starts at the requested time, looked up calendar by calendar
    (each in its own timezone); returns (calendar, event), or Nones.
    """
    for calendar_account in calendar_accounts:
        calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
        _, requested_start, _ = _localize_requested_slot(
            calendar_client, calendar_account, requested_date, requested_time, event_duration_minutes
        )
        _, existing_event = _find_customer_event_starting_at([calendar_account], customer_sender_id, requested_start)
        if existing_event:
            return calendar_account, existing_event
    return None, None


def _compose_booking_outcome_reply(
    booked_calendar_and_event,
    slot_rejection_reasons: list[str],
    calendar_count: int,
    event_title: str,
    requested_datetime_text: str,
) -> str:
    """Text for the model after the booking attempt: why no calendar took the slot, or the confirmation with the
    event id.
    """
    if not booked_calendar_and_event:
        logger.info(f"[st_book_appointment] No event created across {calendar_count} calendar(s). Reasons: {slot_rejection_reasons}")
        logger.info(f"{'#' * 60}\n")
        return _compose_booking_rejection_message(slot_rejection_reasons)

    logger.info(f"[st_book_appointment] event created in {booked_calendar_and_event[0].email}")
    logger.info(f"{'#' * 60}\n")
    return (
        f"Appointment created and confirmed: '{event_title}' at {requested_datetime_text} "
        f"(event_id {booked_calendar_and_event[1]['id']}). Do not create it again."
    )


@llm_tool
async def st_book_appointment(
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
    customer_sender_id = request["sender_id"]

    _log_booking_call(request, date, time, duration_minutes, fullname, email)

    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_book_appointment] No calendar for create event")
        return "We can't create an event for the user"

    logger.info(f"[st_book_appointment] {len(calendar_accounts)} scheduling calendar(s): {[calendar_account.email for calendar_account in calendar_accounts]}")

    # Idempotency: never create the same appointment twice for the same customer.
    duplicate_calendar_account, existing_event = _find_duplicate_booking(
        calendar_accounts, customer_sender_id, date, time, duration_minutes
    )
    if existing_event:
        logger.info(f"[st_book_appointment] {duplicate_calendar_account.email}: appointment {existing_event['id']} already exists, not created again")
        logger.info(f"{'#' * 60}\n")
        return (
            f"This appointment already exists and is confirmed: '{existing_event.get('summary')}' at {date}T{time} "
            f"(event_id {existing_event['id']}). Do not create it again; just confirm it to the customer."
        )

    requested_datetime_text = date + "T" + time
    booked_calendar_and_event, slot_rejection_reasons = _book_event_in_first_free_calendar(
        message_context=request,
        calendar_accounts=calendar_accounts,
        requested_date=date,
        requested_time=time,
        event_title=title,
        event_duration_minutes=duration_minutes,
        customer_email=email,
        customer_full_name=fullname,
        event_description=description,
        customer_sender_id=customer_sender_id,
    )
    return _compose_booking_outcome_reply(
        booked_calendar_and_event, slot_rejection_reasons, len(calendar_accounts), title, requested_datetime_text
    )


# --------------------------------------------------------


def _choose_event_to_cancel(customer_event_triples, calendar_event_id: Optional[str]):
    """(calendar, client, event) to cancel and None, or None and the text for the model when there is no single
    event to cancel: the id is not one of the customer's events, several events and no id, or no event at all.
    """
    customer_events = [calendar_event for _, _, calendar_event in customer_event_triples]

    # In case the user chose a specific event from the list
    if calendar_event_id:
        triple_to_cancel = next(
            (
                (calendar_account, calendar_client, calendar_event)
                for calendar_account, calendar_client, calendar_event in customer_event_triples
                if calendar_event["id"] == calendar_event_id
            ),
            None,
        )
        if not triple_to_cancel:
            logger.info("[st_cancel_appointment] This event doesn't belong to the user.")
            return None, "This event doesn't belong to the user. Let him choose from the list."
        return triple_to_cancel, None

    # In case the user asks to cancel event but didn't provide id
    if len(customer_event_triples) > 1:
        logger.info(" ".join(str(part) for part in ("[st_cancel_appointment] The user have multiple events: ", customer_events)))
        return None, "The user have multiple events, let him choose the one he wants to cancel: " + str(customer_events)
    if len(customer_event_triples) == 1:
        return customer_event_triples[0], None

    logger.info("[st_cancel_appointment] We don't have any event for the user to cancel")
    return None, "We don't have any event for the user to cancel"


@llm_tool
async def st_cancel_appointment(request: MessageContext, event_id: Optional[str] = None) -> str:
    """
    Cancels a scheduled event for the user.

    event_id: The unique identifier of the event to be canceled
    """
    # remove event for the user we not need to delete reminder,
    # because if the use send message, is automatically deleted
    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_cancel_appointment] No calendar for cancel event")
        return "We can't cancel the event for the user"

    # list of (calendar, service, event) for every confirmed future event at the user's location
    customer_event_triples = _collect_customer_upcoming_events(calendar_accounts, request["sender_id"])

    triple_to_cancel, refusal_reply = _choose_event_to_cancel(customer_event_triples, event_id)
    if refusal_reply:
        return refusal_reply

    calendar_account, calendar_client, event_to_cancel = triple_to_cancel
    _delete_event_and_record_cancellation(calendar_account, calendar_client, event_to_cancel, request["customer"])
    logger.info("[st_cancel_appointment] Ok, we have canceled the event for the user")
    return "Ok, we have canceled the event for the user"


# --------------------------------------------------------


def _fetch_customer_upcoming_events(calendar_accounts, customer_sender_id: str) -> list[dict]:
    """Every confirmed future event whose location is the customer's number, listed from now on in each calendar."""
    current_time_iso = datetime.now(timezone.utc).isoformat() + "Z"
    upcoming_events = []

    for calendar_account in calendar_accounts:
        calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
        events_response = calendar_client.events().list(calendarId=calendar_account.email, timeMin=datetime.now().isoformat() + "Z").execute()
        upcoming_events.extend(
            [
                calendar_event
                for calendar_event in events_response.get("items", [])
                if calendar_event.get("status") == "confirmed"
                and calendar_event.get("location") == customer_sender_id
                and calendar_event.get("start", {}).get("dateTime", current_time_iso) > current_time_iso
            ]
        )

    return upcoming_events


def _compose_event_details_reply(upcoming_events: list[dict], calendar_event_id: Optional[str]) -> str:
    """Text for the model: the event chosen by id, all of them when there are several, none, or the only one."""
    # In case the user chose a specific event from the list to get more details about
    if calendar_event_id:
        for calendar_event in upcoming_events:
            if calendar_event_id == calendar_event["id"]:
                logger.info(" ".join(str(part) for part in ("The event details: ", calendar_event)))
                return "The event details: " + str(calendar_event)
    if len(upcoming_events) > 1:
        logger.info(" ".join(str(part) for part in ("The user have multiple events.", upcoming_events)))
        return "The user have multiple events: " + str(upcoming_events)
    if len(upcoming_events) == 0:
        logger.info("The user have no events.")
        return "The user have no events."
    else:
        logger.info(" ".join(str(part) for part in ("The event details: ", upcoming_events)))
        return "The event details: " + str(upcoming_events)


@llm_tool
async def st_find_customer_appointments(request: MessageContext, event_id: Optional[str] = None) -> str:
    """
    Retrieve details about a scheduled event.

    event_id: The unique identifier of the event to retrieve details for
    """
    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_find_customer_appointments] No calendar for events")
        return "There's no events for the user"

    upcoming_events = _fetch_customer_upcoming_events(calendar_accounts, request["sender_id"])
    return _compose_event_details_reply(upcoming_events, event_id)


# --------------------------------------------------------


def _validate_slot_request_format(requested_date: str, requested_time: str) -> str | None:
    """Text for the model when the date is not YYYY-MM-DD or the time is not HH:MM:SS; None when both are well-formed."""
    # Validate date format - must be YYYY-MM-DD
    date_format_pattern = r"^\d{4}-\d{2}-\d{2}$"
    time_format_pattern = r"^\d{2}:\d{2}:\d{2}$"

    if not requested_date or not re.match(date_format_pattern, str(requested_date)):
        logger.error(f"[ERROR st_check_slot_availability] Invalid date format: {requested_date}")
        return f"Invalid date format: '{requested_date}'. Please provide date in YYYY-MM-DD format (e.g., 2026-01-20)."
    if not requested_time or not re.match(time_format_pattern, str(requested_time)):
        logger.error(f"[ERROR st_check_slot_availability] Invalid time format: {requested_time}")
        return f"Invalid time format: '{requested_time}'. Please provide time in HH:MM:SS format (e.g., 14:30:00)."
    return None


def _check_calendar_slot_for_customer(
    calendar_account, customer_sender_id: str, requested_date: str, requested_time: str, event_duration_minutes: int
) -> str | None:
    """Text for the model when this calendar has the slot free, or already holds the customer's own appointment at
    that start; None when the calendar is closed or busy then, so the next calendar is tried.
    """
    calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)
    calendar_timezone_name = calendar_client.calendarList().get(calendarId=calendar_account.email).execute().get("timeZone")

    calendar_timezone = pytz.timezone(calendar_timezone_name)
    slot_start = calendar_timezone.localize(datetime.strptime(requested_date + "T" + requested_time, NAIVE_DATETIME_FORMAT))
    slot_end = slot_start + timedelta(minutes=event_duration_minutes)

    if (
        calendar_account.opening_hours
        and len(calendar_account.opening_hours) > 0
        and (
            not is_datetime_within_opening_hours(slot_start, calendar_account.opening_hours)
            or not is_datetime_within_opening_hours(slot_end, calendar_account.opening_hours)
        )
    ):
        logger.info(f"[st_check_slot_availability] {calendar_account.email}: closed at this time")
        return None

    if is_slot_free_in_calendar(
        calendar_client=calendar_client,
        google_calendar_id=calendar_account.email,
        slot_start_iso=slot_start.isoformat(),
        slot_end_iso=slot_end.isoformat(),
        timezone_name=calendar_timezone_name,
        should_ignore_long_events=getattr(calendar_account, "ignore_long_events", False),
    ):
        logger.info(f"[st_check_slot_availability] {calendar_account.email}: AVAILABLE")
        return "This time slot is available"
    _, own_event = _find_customer_event_starting_at([calendar_account], customer_sender_id, slot_start)
    if own_event:
        logger.info(f"[st_check_slot_availability] {calendar_account.email}: taken by the customer's own {own_event['id']}")
        return (
            f"This time is already booked for this customer: their own appointment '{own_event.get('summary')}' "
            f"(event_id {own_event['id']}). It is confirmed; do not book it again."
        )
    logger.info(f"[st_check_slot_availability] {calendar_account.email}: busy")
    return None


@llm_tool
async def st_check_slot_availability(
    request: MessageContext, date: str, time: str, duration_minutes: int = 30
) -> str:
    """
    Checks availability based on a specific date and time provided by the user.

    date: The specific date for which to check availability, formatted as YYYY-MM-DD
    time: The specific time for which to check availability, formatted as HH:MM:SS
    duration_minutes: Duration of the event in minutes
    """
    if format_error_reply := _validate_slot_request_format(date, time):
        return format_error_reply

    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_check_slot_availability] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    # Available if at least ONE calendar has the slot free — that calendar will receive the event
    for calendar_account in calendar_accounts:
        if slot_reply := _check_calendar_slot_for_customer(
            calendar_account, request["sender_id"], date, time, duration_minutes
        ):
            return slot_reply

    logger.info("[st_check_slot_availability] No calendar available for this slot")
    return "This time slot is not available"


# --------------------------------------------------------


def _validate_date_range_format(range_start_date: str, range_end_date: str) -> str | None:
    """Text for the model when date_from or date_to is not YYYY-MM-DD; None when both are well-formed."""
    # Validate date format - must be YYYY-MM-DD
    date_format_pattern = r"^\d{4}-\d{2}-\d{2}$"
    if not range_start_date or not re.match(date_format_pattern, str(range_start_date)):
        logger.error(f"[ERROR st_list_free_slots_in_range] Invalid date_from format: {range_start_date}")
        return f"Invalid date format for date_from: '{range_start_date}'. Please provide dates in YYYY-MM-DD format (e.g., 2026-01-20)."
    if not range_end_date or not re.match(date_format_pattern, str(range_end_date)):
        logger.error(f"[ERROR st_list_free_slots_in_range] Invalid date_to format: {range_end_date}")
        return f"Invalid date format for date_to: '{range_end_date}'. Please provide dates in YYYY-MM-DD format (e.g., 2026-01-20)."
    return None


def _collect_rendered_free_slots(
    calendar_accounts, range_start_date, range_start_time, range_end_date, range_end_time, tool_name: str
) -> list[dict]:
    """Free slots of all the calendars in the range, rendered as {date, start, end} for the model.

    Shared by st_list_free_slots_in_range and st_list_upcoming_free_slots: its three log lines carry the calling
    tool's name (`tool_name`) as their `[...]` prefix.
    """
    free_slots = _merge_free_slots_across_calendars(calendar_accounts, range_start_date, range_start_time, range_end_date, range_end_time)

    logger.info(f"[{tool_name}] union returned {len(free_slots)} slots")

    rendered_slots = _render_slots_for_model(free_slots)

    logger.info(f"[{tool_name}] After simplify: {len(rendered_slots)} slots")
    logger.info(f"[{tool_name}] First 20 slots: {rendered_slots[:20]}")
    return rendered_slots


@llm_tool
async def st_list_free_slots_in_range(
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
    logger.info(f"[DEBUG st_list_free_slots_in_range] Received arguments: date_from={date_from}, date_to={date_to}, time_from={time_from}, time_to={time_to}")

    if format_error_reply := _validate_date_range_format(date_from, date_to):
        return format_error_reply

    if not time_from:
        time_from = "00:00:00"
    if not time_to:
        time_to = "23:59:59"

    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_list_free_slots_in_range] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    logger.info(f"[st_list_free_slots_in_range] {len(calendar_accounts)} calendar(s): {[calendar_account.email for calendar_account in calendar_accounts]}")

    rendered_slots = _collect_rendered_free_slots(
        calendar_accounts, date_from, time_from, date_to, time_to, "st_list_free_slots_in_range"
    )

    if len(rendered_slots) > 0:
        logger.info(f"[st_list_free_slots_in_range] Returning {len(rendered_slots)} available times")
        return "Available times: " + str(rendered_slots)
    if len(rendered_slots) == 0:
        logger.info("[st_list_free_slots_in_range] No available times.")
        return "No available times."


# --------------------------------------------------------


def _resolve_upcoming_week_dates(calendar_accounts) -> tuple[str, str]:
    """Tomorrow and tomorrow + 6 days as YYYY-MM-DD, "today" taken in the first calendar's timezone."""
    # Use the first calendar's timezone to compute "today" — calendars usually share a tz
    reference_calendar_account = calendar_accounts[0]
    reference_calendar_client = build_calendar_client(reference_calendar_account.access_token, reference_calendar_account.refresh_token)
    calendar_timezone_name = reference_calendar_client.calendarList().get(calendarId=reference_calendar_account.email).execute().get("timeZone")
    logger.info(f"[st_list_upcoming_free_slots] reference timezone (from {reference_calendar_account.email}): {calendar_timezone_name}")

    calendar_timezone = pytz.timezone(calendar_timezone_name)
    today = datetime.now(calendar_timezone).date()
    logger.info(f"[st_list_upcoming_free_slots] today (in calendar timezone): {today}")

    tomorrow = today + timedelta(days=1)
    range_last_day = tomorrow + timedelta(days=6)  # 6 more days after tomorrow

    return tomorrow.strftime("%Y-%m-%d"), range_last_day.strftime("%Y-%m-%d")


def _compose_free_slots_reply(message_context: MessageContext, rendered_slots: list[dict]) -> str:
    """Text for the model: no slot, 2-3 suggestions from different days on a phone call, or the full list."""
    if len(rendered_slots) == 0:
        logger.info("[st_list_upcoming_free_slots] No available times.")
        logger.info(f"{'#' * 60}\n")
        return "No available times."

    # For phone calls, return only 2-3 suggestions from different days
    if message_context.get("platform") == "telephone":
        suggested_slots = _sample_slots_from_distinct_days(rendered_slots)
        logger.info(f"[st_list_upcoming_free_slots] Telephone mode - returning {len(suggested_slots)} suggestions")
        logger.info(f"{'#' * 60}\n")
        return "Suggested available times: " + str(suggested_slots)

    logger.info(f"[st_list_upcoming_free_slots] Returning {len(rendered_slots)} available times")
    logger.info(f"{'#' * 60}\n")
    return "Available times: " + str(rendered_slots)


@llm_tool
async def st_list_upcoming_free_slots(request: MessageContext) -> str:
    """
    Get available times from the calendar for events when no dates/times/indicators are provided by the user.
    """
    logger.info(f"\n{'#' * 60}")
    logger.info("[st_list_upcoming_free_slots] CALLED")
    logger.info(f"[st_list_upcoming_free_slots] tenant_name: {request['tenant_name']}")

    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_list_upcoming_free_slots] No calendar for checking availability")
        return "We can't can't check availability because the calendar doesn't exist"

    logger.info(f"[st_list_upcoming_free_slots] {len(calendar_accounts)} calendar(s): {[calendar_account.email for calendar_account in calendar_accounts]}")

    range_start_date, range_end_date = _resolve_upcoming_week_dates(calendar_accounts)
    range_start_time = "00:00:00"
    range_end_time = "23:59:59"

    logger.info("[st_list_upcoming_free_slots] Calling _get_availability_union with:")
    logger.info(f"[st_list_upcoming_free_slots]   date_from: {range_start_date}")
    logger.info(f"[st_list_upcoming_free_slots]   date_to: {range_end_date}")

    rendered_slots = _collect_rendered_free_slots(
        calendar_accounts, range_start_date, range_start_time, range_end_date, range_end_time, "st_list_upcoming_free_slots"
    )

    return _compose_free_slots_reply(request, rendered_slots)


# --------------------------------------------------------


@llm_tool
async def st_reschedule_appointment(
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

    calendar_accounts = _resolve_scheduling_calendars(request["tenant_name"])
    if not calendar_accounts:
        logger.info("[st_reschedule_appointment] No calendar for rescheduling")
        return "We can't reschedule the event because the calendar doesn't exist."

    target_calendar_account, target_calendar_client, calendar_event = _find_event_across_calendars(
        calendar_accounts, event_id
    )
    if not target_calendar_account or not calendar_event:
        logger.warning(f"[st_reschedule_appointment] Event {event_id} not found in any calendar")
        return "Event not found. Get the events of the user and let him choose which one he wants to reschedule."

    calendar_timezone_name, slot_start, slot_end = _localize_rescheduled_slot(
        target_calendar_client, target_calendar_account, new_date, new_time, duration_minutes
    )
    if slot_conflict_reply := _find_rescheduled_slot_conflict(
        target_calendar_client, target_calendar_account, calendar_timezone_name, slot_start, slot_end
    ):
        return slot_conflict_reply

    try:
        updated_event = _move_event_to_slot(
            target_calendar_client, target_calendar_account, event_id, calendar_event, calendar_timezone_name, slot_start, slot_end
        )
        _record_meeting_new_times(request["customer"], event_id, slot_start, slot_end)
        logger.info(" ".join(str(part) for part in ("Event rescheduled:", updated_event)))
        return f"Ok, I have rescheduled your event to {new_date} at {new_time}."

    except Exception as error:
        logger.error(" ".join(str(part) for part in ("Error during rescheduling:", str(error))))
        return "Something went wrong while trying to reschedule the event."


# --------------------------------------------------------
# Helper functions (not decorated - used internally by the tools above)
# --------------------------------------------------------


def _group_slots_by_day(rendered_slots):
    """The rendered slots grouped by their 'date' string, each day keeping the slots in their order."""
    slots_by_day = {}
    for slot in rendered_slots:
        day = slot["date"]
        if day not in slots_by_day:
            slots_by_day[day] = []
        slots_by_day[day].append(slot)
    return slots_by_day


def _pick_mid_day_slot(day_slots):
    """One slot of the day: a random one from the middle third when the day has 3 or more, otherwise the first."""
    slot_count = len(day_slots)
    if slot_count >= 3:
        middle_third_start = slot_count // 3
        middle_third_end = 2 * slot_count // 3
        return random.choice(day_slots[middle_third_start:middle_third_end])
    return day_slots[0]


def _sample_slots_from_distinct_days(rendered_slots):
    """Pick 2-3 suggested slots from different days (for phone calls)"""
    # Group by day
    slots_by_day = _group_slots_by_day(rendered_slots)

    # Pick up to 3 different days
    available_days = sorted(slots_by_day.keys())
    selected_days = available_days[:3] if len(available_days) <= 3 else random.sample(available_days, 3)
    selected_days.sort()

    # From each day, pick a mid-range slot (not the earliest/latest)
    suggested_slots = []
    for day in selected_days:
        suggested_slots.append(_pick_mid_day_slot(slots_by_day[day]))

    return suggested_slots


def _localize_search_range(
    calendar_client, calendar_account, range_start_date, range_start_time, range_end_date, range_end_time
):
    """Localize both bounds of the range in the calendar's timezone; returns (timezone, range start, range end)."""
    # get the calendar's timezone
    calendar_timezone_name = calendar_client.calendarList().get(calendarId=calendar_account.email).execute().get("timeZone")

    logger.info(f"[_get_availability] calendar_timezone from API: {calendar_timezone_name}")

    # create end datetime
    range_start_text = range_start_date + "T" + range_start_time
    naive_range_start = datetime.strptime(range_start_text, NAIVE_DATETIME_FORMAT)
    range_end_text = range_end_date + "T" + range_end_time
    naive_range_end = datetime.strptime(range_end_text, NAIVE_DATETIME_FORMAT)

    logger.info(f"[_get_availability] date_from_str: {range_start_text}")
    logger.info(f"[_get_availability] date_to_str: {range_end_text}")

    # FIX: Use localize() instead of astimezone() for naive datetimes
    # astimezone() assumes the naive datetime is in system local timezone, which is wrong
    # We want to treat the datetime as being in the calendar's timezone
    calendar_timezone = pytz.timezone(calendar_timezone_name)
    range_start = calendar_timezone.localize(naive_range_start)
    range_end = calendar_timezone.localize(naive_range_end)

    logger.info(f"[_get_availability] date_from_obj (localized): {range_start}")
    logger.info(f"[_get_availability] date_to_obj (localized): {range_end}")
    return calendar_timezone_name, range_start, range_end


def _keep_slots_within_opening_hours(available_slots, calendar_account, calendar_timezone_name):
    """The free slots converted to the calendar's timezone, kept only when start and end are inside the opening hours."""
    slots_within_hours = []
    for slot_start, slot_end in available_slots:
        # Convert the start and end datetime to the correct timezone
        slot_start = slot_start.astimezone(pytz.timezone(calendar_timezone_name))
        slot_end = slot_end.astimezone(pytz.timezone(calendar_timezone_name))

        # Check if the slot is within the opening hours
        if calendar_account.opening_hours and len(calendar_account.opening_hours) > 0:
            # Check if the start and end datetime are within the opening hours
            is_start_within_hours = is_datetime_within_opening_hours(slot_start, calendar_account.opening_hours)
            is_end_within_hours = is_datetime_within_opening_hours(slot_end, calendar_account.opening_hours)

            # If either start or end time is outside the opening hours, skip this slot
            if not is_start_within_hours or not is_end_within_hours:
                continue

            if is_start_within_hours and is_end_within_hours:
                # If the slot is within the opening hours, append it to the result list
                slots_within_hours.append((slot_start, slot_end))
    return slots_within_hours


def _list_free_slots_in_calendar(calendar_account, range_start_date, range_start_time, range_end_date, range_end_time):
    """Get available times for range of dates and times for a single calendar."""
    logger.info(f"\n{'=' * 60}")
    logger.info("[_get_availability] CALLED")
    logger.info(f"[_get_availability] calendar.email: {calendar_account.email}")
    logger.info(f"[_get_availability] calendar.ignore_long_events: {getattr(calendar_account, 'ignore_long_events', 'NOT SET')}")
    logger.info(f"[_get_availability] date_from: {range_start_date}")
    logger.info(f"[_get_availability] time_from: {range_start_time}")
    logger.info(f"[_get_availability] date_to: {range_end_date}")
    logger.info(f"[_get_availability] time_to: {range_end_time}")

    calendar_client = build_calendar_client(calendar_account.access_token, calendar_account.refresh_token)

    calendar_timezone_name, range_start, range_end = _localize_search_range(
        calendar_client, calendar_account, range_start_date, range_start_time, range_end_date, range_end_time
    )

    logger.info("[_get_availability] Calling get_available_time_slots_in_range...")

    # Get available time slots in range of dates
    available_slots = find_free_slots_in_range(
        calendar_client=calendar_client,
        google_calendar_id=calendar_account.email,
        range_start_iso=range_start.isoformat(),
        range_end_iso=range_end.isoformat(),
        timezone_name=calendar_timezone_name,
        should_ignore_long_events=getattr(calendar_account, "ignore_long_events", False),
    )

    logger.info(f"[_get_availability] get_available_time_slots_in_range returned {len(available_slots)} slots")

    slots_within_hours = _keep_slots_within_opening_hours(available_slots, calendar_account, calendar_timezone_name)

    logger.info(f"[_get_availability] After opening hours filter: {len(slots_within_hours)} slots")
    logger.info(f"{'=' * 60}\n")

    return slots_within_hours


def _merge_free_slots_across_calendars(calendar_accounts, range_start_date, range_start_time, range_end_date, range_end_time):
    """Union of available slots across multiple calendars.

    A slot is included if at least one calendar has it free. This matches the
    scheduling logic — the event will be created in whichever calendar(s) are
    available at that moment.

    Slots are deduplicated by their UTC instant so calendars in different
    timezones still merge correctly. The result is sorted by start time.
    """
    seen_slot_keys = set()
    merged_slots = []
    for calendar_account in calendar_accounts:
        calendar_free_slots = _list_free_slots_in_calendar(
            calendar_account, range_start_date, range_start_time, range_end_date, range_end_time
        )
        for slot_start, slot_end in calendar_free_slots:
            slot_key = (slot_start.astimezone(pytz.UTC).isoformat(), slot_end.astimezone(pytz.UTC).isoformat())
            if slot_key in seen_slot_keys:
                continue
            seen_slot_keys.add(slot_key)
            merged_slots.append((slot_start, slot_end))
    merged_slots.sort(key=lambda slot_window: slot_window[0])
    return merged_slots


def _render_slots_for_model(slot_windows):
    """(start, end) datetimes to {date, start, end} strings the model can read."""
    return [
        {"date": slot_start.strftime("%d-%m-%Y"), "start": slot_start.strftime("%H:%M"), "end": slot_end.strftime("%H:%M")}
        for slot_start, slot_end in slot_windows
    ]
