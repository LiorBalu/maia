"""Admin API: Google account OAuth (calendar + Gmail scopes), connected accounts, opening hours, calendar events."""

import logging
import json
import os
import uuid
from datetime import datetime, timedelta
from os import getenv
from typing import Annotated, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pytz
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from mongoengine.errors import NotUniqueError
from pydantic import BaseModel

from maia.services.registry import resolve_existing_tenant_name
from maia.models.users import User
from maia.models.google_accounts import GoogleShared, OpeningHours, OpeningHoursModel
from maia.api.security import authenticate_current_user
from maia.utils.CustomJSONResponse import CustomJSONResponse
from maia.api.email import RegisterEmail, build_gmail_client, register_gmail_watch

logger = logging.getLogger(__name__)

GOOGLE_OAUTH_CLIENT_ID = getenv("GOOGLE_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = getenv("GOOGLE_CLIENT_SECRET")
CALENDAR_API_SCOPES = ["https://www.googleapis.com/auth/calendar"]
# The consent flow asks Google for incremental authorization, so the token returned when an account
# connects Gmail also covers the calendar scopes it granted earlier (and vice versa). oauthlib
# raises on that "scope changed" response unless told to accept it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


calendar_router = APIRouter(prefix="/calendar", tags=["Calendar"])


BACKEND_BASE_URL = getenv("BACKEND_URL", "http://localhost:8000")

OAUTH_CALLBACK_URL = f"{BACKEND_BASE_URL}/management/v1/calendar/oauth2callback"

default_oauth_flow = Flow.from_client_config(
    {
        "web": {
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_CALLBACK_URL],
        }
    },
    scopes=[
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    redirect_uri=OAUTH_CALLBACK_URL,
)

SCOPES_BY_INTEGRATION = {
    "calendar": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
    "email": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
    "meet": [
        # Read past conference records + their recordings/transcripts artifacts.
        "https://www.googleapis.com/auth/meetings.tenant.readonly",
        # Drive read is needed to locate/export the Gemini "meeting notes" Doc.
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ],
}


def build_calendar_client(access_token: str, refresh_token: str):
    """Google Calendar API client for the stored OAuth tokens (the access token is refreshed automatically)."""
    oauth_credentials = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=CALENDAR_API_SCOPES,
    )
    return build("calendar", "v3", credentials=oauth_credentials)


def _list_events_in_window(calendar_client, google_calendar_id, slot_start_iso, slot_end_iso) -> list[dict] | None:
    """One Events List page (up to 50 events) of the calendar between the slot bounds; None when the API call fails,
    after logging the 'NOT AVAILABLE' fallback, so the caller answers False.
    """
    try:
        events_page = (
            calendar_client.events()
            .list(
                calendarId=google_calendar_id,
                timeMin=slot_start_iso,
                timeMax=slot_end_iso,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,  # Small range, shouldn't have many events
            )
            .execute()
        )

        window_events = events_page.get("items", [])
        logger.info(f"[is_slot_free_in_calendar] Events List API returned {len(window_events)} events")

    except Exception as error:
        logger.error(f"[is_slot_free_in_calendar] ERROR calling Events List API: {error}")
        # Fallback: assume NOT available if API fails (safer - prevents double bookings)
        logger.error("[is_slot_free_in_calendar] Fallback -> NOT AVAILABLE (False) - to prevent double bookings")
        logger.error(f"{'=' * 60}\n")
        return None

    return window_events


def _parse_event_interval(event: dict, calendar_timezone) -> tuple[datetime, datetime]:
    """Start and end of one Google event: an all-day event ('date') runs from midnight to midnight in the calendar's
    timezone, a timed event ('dateTime') carries its own offset.
    """
    # Get start and end times
    start_field = event.get("start", {})
    end_field = event.get("end", {})

    # Handle all-day events (have 'date' instead of 'dateTime')
    if "date" in start_field:
        # All-day event - date format is YYYY-MM-DD
        all_day_start_iso = start_field["date"] + "T00:00:00"
        all_day_end_iso = end_field["date"] + "T00:00:00"

        # Parse and add timezone (all-day events don't have timezone info)
        event_start = calendar_timezone.localize(datetime.fromisoformat(all_day_start_iso))
        event_end = calendar_timezone.localize(datetime.fromisoformat(all_day_end_iso))
    else:
        # Regular event with specific time - has timezone in dateTime string
        event_start = datetime.fromisoformat(start_field.get("dateTime"))
        event_end = datetime.fromisoformat(end_field.get("dateTime"))

    return event_start, event_end


def _extract_busy_interval(
    event: dict, calendar_timezone, should_ignore_long_events: bool, log_prefix: str
) -> tuple[datetime, datetime] | None:
    """The (start, end) an event keeps busy, or None when it does not count: cancelled, transparent (marked free) or,
    with should_ignore_long_events, longer than 12 hours. Every check is logged under the caller's log_prefix.
    """
    # Skip cancelled events
    if event.get("status") == "cancelled":
        logger.warning(f"{log_prefix}   Skipping cancelled event: {event.get('summary', 'No title')}")
        return None

    # Skip transparent events (marked as "free" / "available")
    # transparency: 'opaque' = busy (default), 'transparent' = free
    if event.get("transparency") == "transparent":
        logger.warning(f"{log_prefix}   Skipping transparent (free) event: {event.get('summary', 'No title')}")
        return None

    event_start, event_end = _parse_event_interval(event, calendar_timezone)

    # Calculate duration
    duration_hours = (event_end - event_start).total_seconds() / 3600
    event_summary = event.get("summary", "No title")

    logger.info(f"{log_prefix}   Event: '{event_summary}' | {event_start} to {event_end} ({duration_hours:.1f}h)")

    # Filter out long events (> 12 hours) if the setting is enabled
    if should_ignore_long_events and duration_hours > 12:
        logger.info(f"{log_prefix}     -> FILTERED (>12h, ignore_long_events=True)")
        return None

    return event_start, event_end


def _find_blocking_events(
    window_events: list[dict],
    requested_slot_start,
    requested_slot_end,
    calendar_timezone,
    should_ignore_long_events: bool,
) -> list[str]:
    """Titles of the busy events that overlap the requested slot; every event and the total are logged."""
    # Check each event for overlap with the requested slot
    blocking_event_titles = []

    for event in window_events:
        busy_interval = _extract_busy_interval(
            event, calendar_timezone, should_ignore_long_events, "[is_slot_free_in_calendar]"
        )
        if busy_interval is None:
            continue
        event_start, event_end = busy_interval

        # Check if event overlaps with the requested slot
        # Overlap occurs if: event_start < requested_slot_end AND event_end > requested_slot_start
        if event_start < requested_slot_end and event_end > requested_slot_start:
            logger.info("[is_slot_free_in_calendar]     -> BLOCKS the requested slot")
            blocking_event_titles.append(event.get("summary", "No title"))
        else:
            logger.info("[is_slot_free_in_calendar]     -> Does not block (no overlap)")

    logger.info(f"[is_slot_free_in_calendar] Total blocking events: {len(blocking_event_titles)}")
    return blocking_event_titles


def is_slot_free_in_calendar(calendar_client, google_calendar_id, slot_start_iso, slot_end_iso, timezone_name, should_ignore_long_events=False):
    """
    Check if a specific time slot is available.
    Uses Events List API (same as find_free_slots_in_range) for consistency.
    FreeBusy API can include events from subscribed/shared calendars which causes mismatches.
    """

    logger.info(f"\n{'=' * 60}")
    logger.info("[is_slot_free_in_calendar] CALLED")
    logger.info(f"[is_slot_free_in_calendar] calendar_id: {google_calendar_id}")
    logger.info(f"[is_slot_free_in_calendar] start_time: {slot_start_iso}")
    logger.info(f"[is_slot_free_in_calendar] end_time: {slot_end_iso}")
    logger.info(f"[is_slot_free_in_calendar] timezone: {timezone_name}")
    logger.info(f"[is_slot_free_in_calendar] ignore_long_events: {should_ignore_long_events}")

    # Use Events List API instead of FreeBusy for consistency with find_free_slots_in_range
    # FreeBusy can include events from subscribed/shared calendars causing mismatches
    logger.info("[is_slot_free_in_calendar] Using Events List API for consistency...")

    window_events = _list_events_in_window(calendar_client, google_calendar_id, slot_start_iso, slot_end_iso)
    if window_events is None:
        return False

    # Parse the requested time slot for overlap checking
    requested_slot_start = datetime.fromisoformat(slot_start_iso)
    requested_slot_end = datetime.fromisoformat(slot_end_iso)
    calendar_timezone = pytz.timezone(timezone_name)

    blocking_event_titles = _find_blocking_events(
        window_events, requested_slot_start, requested_slot_end, calendar_timezone, should_ignore_long_events
    )

    if not blocking_event_titles:
        logger.info("[is_slot_free_in_calendar] No blocking events -> AVAILABLE (True)")
        logger.info(f"{'=' * 60}\n")
        return True

    logger.info(f"[is_slot_free_in_calendar] Has {len(blocking_event_titles)} blocking events -> NOT AVAILABLE (False)")
    logger.info(f"[is_slot_free_in_calendar] Blocking events: {blocking_event_titles}")
    logger.info(f"{'=' * 60}\n")
    return False


def _list_events_in_range(calendar_client, google_calendar_id, range_start_iso, range_end_iso) -> list[dict]:
    """Every event of the calendar between the range bounds, page by page (250 per page); [] when the Events List
    API call fails (the error is logged).
    """
    range_events = []
    page_token = None

    try:
        # Handle pagination - events().list() may return multiple pages
        while True:
            events_page = (
                calendar_client.events()
                .list(
                    calendarId=google_calendar_id,
                    timeMin=range_start_iso,
                    timeMax=range_end_iso,
                    singleEvents=True,  # Expand recurring events into individual instances
                    orderBy="startTime",
                    pageToken=page_token,
                    maxResults=250,  # Max allowed per page
                )
                .execute()
            )

            range_events.extend(events_page.get("items", []))
            page_token = events_page.get("nextPageToken")

            if not page_token:
                break

        logger.info(f"[find_free_slots_in_range] Events List API returned {len(range_events)} events")

    except Exception as error:
        logger.error(f"[find_free_slots_in_range] ERROR calling Events List API: {error}")
        range_events = []

    return range_events


def _collect_busy_intervals(range_events: list[dict], calendar_timezone, should_ignore_long_events: bool) -> list[dict]:
    """The busy intervals of the events as {'start', 'end'} ISO strings, in the events' order (cancelled, transparent
    and, when asked, longer-than-12-hours events are left out).
    """
    # Convert events to busy slots format
    busy_intervals = []

    for event in range_events:
        busy_interval = _extract_busy_interval(
            event, calendar_timezone, should_ignore_long_events, "[find_free_slots_in_range]"
        )
        if busy_interval is None:
            continue
        event_start, event_end = busy_interval

        logger.info("[find_free_slots_in_range]     -> KEPT as busy slot")

        busy_intervals.append({"start": event_start.isoformat(), "end": event_end.isoformat()})

    logger.info(f"[find_free_slots_in_range] Total busy slots after filtering: {len(busy_intervals)}")
    return busy_intervals


def _generate_free_slots(
    sorted_busy_intervals: list[dict], range_start_iso, range_end_iso, slot_length_minutes
) -> list[tuple[datetime, datetime]]:
    """Walk a cursor from the range start through the sorted busy intervals, adding a slot_length_minutes slot for
    every slot that fits in a gap and after the last interval up to the range end.
    """
    # List to store available slots
    free_slots = []

    # Initialize cursor_time to the range_start_iso
    cursor_time = datetime.fromisoformat(range_start_iso)

    logger.info("[find_free_slots_in_range] Starting slot generation...")
    logger.info(f"[find_free_slots_in_range] current_time initialized to: {cursor_time}")

    # Iterate through the busy slots and find gaps
    for busy_index, busy_interval in enumerate(sorted_busy_intervals):
        busy_start = datetime.fromisoformat(busy_interval["start"])
        busy_end = datetime.fromisoformat(busy_interval["end"])

        logger.info(
            f"[find_free_slots_in_range] Processing busy_slot[{busy_index}]: {busy_start} to {busy_end}"
        )
        logger.info(f"[find_free_slots_in_range]   current_time before loop: {cursor_time}")

        gap_slot_count = 0
        # Check if there's a gap between cursor_time and the busy start time
        while cursor_time + timedelta(minutes=slot_length_minutes) <= busy_start:
            free_slots.append((cursor_time, cursor_time + timedelta(minutes=slot_length_minutes)))
            gap_slot_count += 1
            cursor_time += timedelta(minutes=slot_length_minutes)

        logger.info(
            f"[find_free_slots_in_range]   Added {gap_slot_count} available slots before this busy slot"
        )

        # Update cursor_time to be after the busy slot
        previous_cursor_time = cursor_time
        cursor_time = max(cursor_time, busy_end)
        logger.info(
            f"[find_free_slots_in_range]   current_time updated: {previous_cursor_time} -> {cursor_time}"
        )

    logger.info(f"[find_free_slots_in_range] After processing all busy slots, current_time: {cursor_time}")

    # After the last busy slot, check for available slots until the range_end_iso
    trailing_slot_count = 0
    while cursor_time + timedelta(minutes=slot_length_minutes) <= datetime.fromisoformat(range_end_iso):
        free_slots.append((cursor_time, cursor_time + timedelta(minutes=slot_length_minutes)))
        trailing_slot_count += 1
        cursor_time += timedelta(minutes=slot_length_minutes)

    logger.info(f"[find_free_slots_in_range] Added {trailing_slot_count} slots after last busy slot")
    logger.info(f"[find_free_slots_in_range] Total available_slots generated: {len(free_slots)}")
    return free_slots


def find_free_slots_in_range(
    calendar_client, google_calendar_id, range_start_iso, range_end_iso, timezone_name, slot_length_minutes=30, should_ignore_long_events=False
):
    """
    Retrieve available time slots within a range of dates.
    Each slot has a duration of slot_length_minutes.
    If should_ignore_long_events is True, events longer than 12 hours are ignored.

    IMPORTANT: We use the Events List API instead of FreeBusy to get ACTUAL events,
    not merged busy blocks. FreeBusy merges events when all-day events exist.
    """

    logger.info(f"\n{'=' * 60}")
    logger.info("[find_free_slots_in_range] CALLED")
    logger.info(f"[find_free_slots_in_range] calendar_id: {google_calendar_id}")
    logger.info(f"[find_free_slots_in_range] start_date: {range_start_iso}")
    logger.info(f"[find_free_slots_in_range] end_date: {range_end_iso}")
    logger.info(f"[find_free_slots_in_range] timezone: {timezone_name}")
    logger.info(f"[find_free_slots_in_range] slot_duration_minutes: {slot_length_minutes}")
    logger.info(f"[find_free_slots_in_range] ignore_long_events: {should_ignore_long_events}")

    # Use Events List API instead of FreeBusy to get actual events (not merged blocks)
    logger.info("[find_free_slots_in_range] Using Events List API to get actual events...")

    range_events = _list_events_in_range(calendar_client, google_calendar_id, range_start_iso, range_end_iso)

    calendar_timezone = pytz.timezone(timezone_name)
    busy_intervals = _collect_busy_intervals(range_events, calendar_timezone, should_ignore_long_events)

    # Sort busy_intervals by start time - algorithm requires sorted slots!
    sorted_busy_intervals = sorted(busy_intervals, key=lambda busy_interval: busy_interval["start"])
    logger.info(f"[find_free_slots_in_range] busy_slots AFTER sort: {sorted_busy_intervals}")

    free_slots = _generate_free_slots(sorted_busy_intervals, range_start_iso, range_end_iso, slot_length_minutes)
    logger.info(f"[find_free_slots_in_range] First 10 slots: {free_slots[:10]}")
    logger.info(f"{'=' * 60}\n")

    return free_slots


def _resolve_requested_scopes(scopes_csv: str) -> list[str]:
    """Google scope URLs for the comma-separated integration keys (calendar / email / meet), deduplicated, with
    'openid' always added.
    """
    requested_scope_urls = []

    for integration_key in scopes_csv.split(","):
        normalized_integration_key = integration_key.strip().lower()
        if normalized_integration_key in SCOPES_BY_INTEGRATION:
            requested_scope_urls.extend(SCOPES_BY_INTEGRATION[normalized_integration_key])
    requested_scope_urls = list(set(requested_scope_urls))
    if "openid" not in requested_scope_urls:
        requested_scope_urls.append("openid")
    return requested_scope_urls


def _build_oauth_flow(requested_scope_urls: list[str]) -> Flow:
    """OAuth flow of the web client for the given scopes, redirecting to OAUTH_CALLBACK_URL."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [OAUTH_CALLBACK_URL],
            }
        },
        scopes=requested_scope_urls,
        redirect_uri=OAUTH_CALLBACK_URL,
    )


def _attach_code_verifier_to_state(authorization_url: str, state_json: str, code_verifier: str | None) -> str:
    """The consent URL with the PKCE code_verifier added to its JSON state, so the callback can restore it."""
    # Store the code_verifier in the state so we can use it in the callback (required for PKCE)
    state_fields = json.loads(state_json)
    state_fields["code_verifier"] = code_verifier
    state_with_verifier_json = json.dumps(state_fields)
    # Replace the state parameter in the authorization URL
    parsed_authorization_url = urlparse(authorization_url)
    query_fields = parse_qs(parsed_authorization_url.query)
    query_fields["state"] = [state_with_verifier_json]
    query_with_verifier = urlencode(query_fields, doseq=True)
    return urlunparse(parsed_authorization_url._replace(query=query_with_verifier))


@calendar_router.get("/authorization-url/{tenant_name}")
async def start_google_oauth(
    tenant_name: str,
    http_request: Request,
    scopes: Optional[str] = Query(""),
    pub_sub: bool = Query(False),
    current_user: User = Depends(authenticate_current_user),
):
    """Build the Google consent URL for the requested scopes (calendar / email / meet). The tenant, the scopes and the
    PKCE code_verifier travel in the OAuth state; the dashboard opens the URL in a popup.
    """
    oauth_state_json = json.dumps({"tenant_name": tenant_name, "scopes": scopes, "pub_sub": pub_sub})

    requested_scope_urls = _resolve_requested_scopes(scopes)

    if not requested_scope_urls:
        return JSONResponse(status_code=400, content={"error": "No valid scopes provided."})

    oauth_flow = _build_oauth_flow(requested_scope_urls)

    consent_url, returned_state_json = oauth_flow.authorization_url(
        prompt="consent", state=oauth_state_json, access_type="offline", include_granted_scopes="true"
    )
    consent_url = _attach_code_verifier_to_state(consent_url, returned_state_json, oauth_flow.code_verifier)
    return JSONResponse(content={"authorization_url": consent_url})


def _union_scopes(stored_scopes: list[str] | None, granted_scopes: list[str]) -> list[str]:
    """Union of the scopes stored on a GoogleShared record and the ones granted in this consent."""
    return sorted(set(stored_scopes or []) | set(granted_scopes))


def _render_oauth_popup(posted_payload, page_text: str) -> HTMLResponse:
    """Render a popup HTML that posts `posted_payload` to opener and closes itself."""
    posted_payload_json = json.dumps(posted_payload)
    popup_html = f"""
    <html>
        <head>
            <script>
                window.onload = function() {{
                    if (window.opener) {{
                        window.opener.postMessage({posted_payload_json}, '*');
                    }}
                    window.close();
                }};
            </script>
        </head>
        <body>
            <p>{page_text}</p>
        </body>
    </html>
    """
    return HTMLResponse(content=popup_html, status_code=200)


def _render_success_popup(warning_text: str = "") -> HTMLResponse:
    """Success popup. When `warning_text` is provided, sends a structured payload so the
    front can show both a success toast and a warning toast (partial success)."""
    if warning_text:
        posted_payload = {"type": "auth_success", "warning": warning_text}
        page_text = warning_text
    else:
        posted_payload = "auth_success"
        page_text = "Authentication successful! You can close this window."
    return _render_oauth_popup(posted_payload, page_text)


def _render_error_popup(error_text: str) -> HTMLResponse:
    """Error popup. Sends `{type:'auth_error', message}` so the front displays it."""
    return _render_oauth_popup({"type": "auth_error", "message": error_text}, error_text)


def _exchange_authorization_code(oauth_flow: Flow, code_verifier: str | None, authorization_code: str) -> Credentials:
    """Trade the authorization code for the account's tokens, with the PKCE code_verifier restored from the state."""
    # Restore the PKCE code_verifier from state
    if code_verifier:
        oauth_flow.code_verifier = code_verifier
    oauth_flow.fetch_token(code=authorization_code)
    credentials = oauth_flow.credentials
    logger.info(" ".join(str(part) for part in ("credential: ", credentials)))
    return credentials


def _fetch_account_email(credentials: Credentials) -> str | None:
    """Email address of the Google account that gave consent (oauth2 v2 userinfo)."""
    oauth2_client = build("oauth2", "v2", credentials=credentials)
    account_info = oauth2_client.userinfo().get().execute()
    return account_info.get("email")


def _find_primary_calendar_id(account_calendars: list[dict], account_email: str | None) -> str | None:
    """Id of the account's primary calendar, or the account email when no calendar is marked primary."""
    # Identify the primary calendar email (used as default)
    for calendar_entry in account_calendars:
        if calendar_entry.get("primary") is True:
            return calendar_entry["id"]
    return account_email


def _upsert_calendar_record(
    tenant_name: str, calendar_id: str, credentials: Credentials, requested_scope_urls: list[str], is_default: bool
) -> bool:
    """Update the tenant's GoogleShared record of the calendar (tokens, merged scopes, default flag) or create it;
    False when the calendar is already linked to another tenant.
    """
    # One record per account and tenant (`email` is unique across the collection): when the
    # account was connected for Gmail first, merge the calendar scopes into that record.
    existing_record = GoogleShared.objects(tenant_name=tenant_name, email=calendar_id).first()

    if existing_record:
        # Update credentials and merge scopes on the existing entry
        existing_record.update(
            access_token=credentials.token,
            refresh_token=credentials.refresh_token or existing_record.refresh_token,
            expires_at=credentials.expiry,
            scopes=_union_scopes(existing_record.scopes, requested_scope_urls),
            default=is_default,
        )
        existing_record.reload()
        logger.info(f"[oauth2callback] Updated calendar: {calendar_id} (default={is_default})")
        return True

    try:
        GoogleShared(
            tenant_name=tenant_name,
            email=calendar_id,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            expires_at=credentials.expiry,
            scopes=requested_scope_urls,
            default=is_default,
        ).save()
        logger.info(f"[oauth2callback] Created calendar: {calendar_id} (default={is_default})")
    except NotUniqueError:
        # Calendar email is already linked to another tenant — skip it and continue
        logger.error(f"[oauth2callback] Skipped {calendar_id}: already connected to another tenant")
        return False
    return True


def _keep_single_default_calendar(
    tenant_name: str, primary_calendar_id: str | None, linked_calendar_ids: list[str]
) -> None:
    """Clear the default flag on the tenant's other calendar records, once the primary calendar was saved or
    updated.
    """
    # Ensure only the primary has default=True among calendar entries for this tenant
    # (only when the primary was actually saved/updated, otherwise this would clear defaults wrongly)
    if primary_calendar_id in linked_calendar_ids:
        GoogleShared.objects(tenant_name=tenant_name, email__ne=primary_calendar_id).filter(
            scopes__contains="https://www.googleapis.com/auth/calendar"
        ).update(default=False)


def _render_calendar_link_outcome(
    linked_calendar_ids: list[str], calendar_ids_linked_to_other_tenant: list[str]
) -> HTMLResponse:
    """Popup of the calendar consent: an error when every calendar is linked to another tenant, a success with a
    warning when only some are, a plain success otherwise.
    """
    # Decide the popup response based on partial / full / total failure
    if not linked_calendar_ids and calendar_ids_linked_to_other_tenant:
        return _render_error_popup(
            "All selected calendars are already connected to another tenant: "
            + ", ".join(calendar_ids_linked_to_other_tenant)
        )
    if calendar_ids_linked_to_other_tenant:
        return _render_success_popup(
            warning_text=(
                "Some calendars were skipped because they are already connected to "
                "another tenant: " + ", ".join(calendar_ids_linked_to_other_tenant)
            )
        )
    return _render_success_popup()


def _link_account_calendars(
    tenant_name: str, credentials: Credentials, requested_scope_urls: list[str], account_email: str | None
) -> HTMLResponse:
    """Calendar consent: save or update one GoogleShared record per calendar of the account (the primary one becomes
    the default) and answer with the popup that reports the calendars already linked to another tenant.
    """
    # Build calendar service and fetch ALL user calendars
    calendar_client = build("calendar", "v3", credentials=credentials)
    calendar_list_page = calendar_client.calendarList().list().execute()
    account_calendars = calendar_list_page.get("items", [])

    primary_calendar_id = _find_primary_calendar_id(account_calendars, account_email)

    logger.info(f"[oauth2callback] Found {len(account_calendars)} calendars. Primary: {primary_calendar_id}")

    linked_calendar_ids: list[str] = []
    calendar_ids_linked_to_other_tenant: list[str] = []

    # Save each calendar as a separate GoogleShared entry
    for calendar_entry in account_calendars:
        calendar_id = calendar_entry["id"]
        is_default = calendar_id == primary_calendar_id
        if _upsert_calendar_record(tenant_name, calendar_id, credentials, requested_scope_urls, is_default):
            linked_calendar_ids.append(calendar_id)
        else:
            calendar_ids_linked_to_other_tenant.append(calendar_id)

    _keep_single_default_calendar(tenant_name, primary_calendar_id, linked_calendar_ids)

    return _render_calendar_link_outcome(linked_calendar_ids, calendar_ids_linked_to_other_tenant)


def _upsert_gmail_record(
    tenant_name: str, account_email: str | None, credentials: Credentials, requested_scope_urls: list[str]
) -> bool:
    """Update the tenant's GoogleShared record of the Gmail account (tokens, merged scopes) or create it; False when
    the email is already linked to another tenant.
    """
    # One record per account and tenant (`email` is unique across the collection): when the
    # account was connected for the calendar first, merge the Gmail scopes into that record.
    existing_record = GoogleShared.objects(tenant_name=tenant_name, email=account_email).first()

    if existing_record:
        existing_record.update(
            access_token=credentials.token,
            refresh_token=credentials.refresh_token or existing_record.refresh_token,
            expires_at=credentials.expiry,
            scopes=_union_scopes(existing_record.scopes, requested_scope_urls),
        )
        existing_record.reload()
        logger.info(f"[oauth2callback] Updated email entry: {account_email}")
        return True

    try:
        GoogleShared(
            tenant_name=tenant_name,
            email=account_email,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            expires_at=credentials.expiry,
            scopes=requested_scope_urls,
        ).save()
        logger.info(f"[oauth2callback] Created email entry: {account_email}")
    except NotUniqueError:
        return False
    return True


async def _register_gmail_watch_after_link(
    tenant_name: str, account_email: str | None, http_request: Request
) -> str | None:
    """Register the Gmail watch (Pub/Sub push) of the account just linked; returns the warning to show when it could
    not be set up, None when it was.
    """
    try:
        watch_registration = RegisterEmail(email=account_email)

        class MockUser:
            """Minimal stand-in for the user dependency of register_gmail_watch (only tenants_name is read)."""
            def __init__(self, tenant_name):
                """Store the tenant as the user's only tenant."""
                self.tenants_name = [tenant_name]

        dashboard_user_stand_in = MockUser(tenant_name)
        watch_response = await register_gmail_watch(
            http_request=http_request,
            registration=watch_registration,
            tenant_name=tenant_name,
            current_user=dashboard_user_stand_in,
        )
        if getattr(watch_response, "status_code", 200) >= 400:
            error_body = json.loads(watch_response.body)
            error_detail = error_body.get("error") or error_body.get("message") or ""
            logger.info(f"[oauth2callback] Gmail watch not set up for {account_email}: {error_detail}")
            return f"Receiving mail is not active yet: {error_detail}"
        logger.info(f"[oauth2callback] Gmail watch setup successful for {account_email}")
    except Exception as error:
        logger.error(f"[oauth2callback] Error setting up Gmail watch: {error}")
        return f"Receiving mail is not active yet: {error}"
    return None


async def _link_gmail_account(
    tenant_name: str,
    credentials: Credentials,
    requested_scope_urls: list[str],
    account_email: str | None,
    should_register_watch: bool,
    http_request: Request,
) -> HTMLResponse:
    """Gmail consent: save or update the account's GoogleShared record, then register the Gmail watch when asked; a
    watch that cannot be set up only adds a warning to the success popup.
    """
    # For email scope, save a single entry for the user's email
    if not _upsert_gmail_record(tenant_name, account_email, credentials, requested_scope_urls):
        return _render_error_popup(f"Email {account_email} is already connected to another tenant.")

    # Gmail watch (Pub/Sub push) if requested. Runs after the record is saved so the registration
    # finds this account's tokens. It needs a public HTTPS BACKEND_URL; when it cannot be set up
    # the account stays connected and the popup reports that receiving mail is not active yet.
    if should_register_watch:
        watch_warning_text = await _register_gmail_watch_after_link(tenant_name, account_email, http_request)
        if watch_warning_text is not None:
            return _render_success_popup(warning_text=watch_warning_text)
    return _render_success_popup()


def _link_meet_account(
    tenant_name: str, credentials: Credentials, requested_scope_urls: list[str], account_email: str | None
) -> HTMLResponse:
    """Meet consent: merge the Meet scopes into the account's existing GoogleShared record or create a meet-scoped
    one.
    """
    # Save a single meet-scoped entry for the connected account (the meeting organizer).
    # The Meet poller reads this entry to pull conference records + artifacts.
    # `email` is globally unique on GoogleShared, so if this account is already connected
    # for the SAME tenant (e.g. calendar), merge the meet scopes into that entry instead of
    # creating a colliding doc.
    existing_record = GoogleShared.objects(tenant_name=tenant_name, email=account_email).first()
    if existing_record:
        merged_scope_urls = list(set((existing_record.scopes or []) + requested_scope_urls))
        existing_record.update(
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            expires_at=credentials.expiry,
            scopes=merged_scope_urls,
        )
        existing_record.reload()
        logger.info(f"[oauth2callback] Merged meet scopes into existing entry: {account_email}")
    else:
        try:
            GoogleShared(
                tenant_name=tenant_name,
                email=account_email,
                access_token=credentials.token,
                refresh_token=credentials.refresh_token,
                expires_at=credentials.expiry,
                scopes=requested_scope_urls,
            ).save()
            logger.info(f"[oauth2callback] Created meet entry: {account_email}")
        except NotUniqueError:
            return _render_error_popup(f"Account {account_email} is already connected to another tenant.")
    return _render_success_popup()


@calendar_router.get("/oauth2callback")
async def complete_google_oauth(http_request: Request):
    """Google redirects here after consent: exchange the code for tokens, read the account email and save or update
    the GoogleShared records (every calendar of the account for the calendar scope, one record for Gmail / Meet),
    then register the Gmail watch when asked. Answers with a popup page that posts the result to the dashboard
    window.
    """
    callback_query_params = http_request.query_params
    oauth_state_json = callback_query_params.get("state")
    if not oauth_state_json:
        return JSONResponse(status_code=400, content={"error": "State is missing."})

    oauth_state = json.loads(oauth_state_json)

    tenant_name = oauth_state.get("tenant_name")
    integration_keys_csv = oauth_state.get("scopes", "")
    should_register_watch = oauth_state.get("pub_sub", False)
    requested_integration_keys = integration_keys_csv.split(",")
    if not tenant_name:
        return JSONResponse(status_code=400, content={"error": "Tenant name is missing."})
    logger.info(" ".join(str(part) for part in ("Headers: ", http_request.headers)))
    logger.info(" ".join(str(part) for part in ("URL: ", http_request.url)))
    requested_scope_urls = _resolve_requested_scopes(integration_keys_csv)
    if not requested_scope_urls:
        return JSONResponse(status_code=400, content={"error": "No valid scopes provided."})

    oauth_flow = _build_oauth_flow(requested_scope_urls)
    code_verifier = oauth_state.get("code_verifier")
    # Extract the authorization code directly from query params
    # (avoids URL host mismatch issues with proxies/tunnels)
    authorization_code = http_request.query_params.get("code")
    if not authorization_code:
        return JSONResponse(status_code=400, content={"error": "Authorization code is missing."})
    credentials = _exchange_authorization_code(oauth_flow, code_verifier, authorization_code)
    account_email = _fetch_account_email(credentials)

    if "calendar" in requested_integration_keys:
        return _link_account_calendars(tenant_name, credentials, requested_scope_urls, account_email)
    elif "email" in requested_integration_keys:
        return await _link_gmail_account(
            tenant_name, credentials, requested_scope_urls, account_email, should_register_watch, http_request
        )
    elif "meet" in requested_integration_keys:
        return _link_meet_account(tenant_name, credentials, requested_scope_urls, account_email)
    return _render_success_popup()


@calendar_router.get("/all/{tenant_name}")
async def list_all_google_accounts(tenant_name: str, current_user: User = Depends(authenticate_current_user)):
    """All GoogleShared records (connected Google accounts / calendars) of the tenant, raw."""
    account_records = GoogleShared.objects(tenant_name=tenant_name)
    return CustomJSONResponse(content=[account_record.to_mongo().to_dict() for account_record in account_records])


# Scope fragment used by /connected to bucket a GoogleShared record per integration card
SCOPE_FRAGMENT_BY_INTEGRATION = {"calendar": "calendar", "email": "gmail", "meet": "meetings"}


def _has_integration_scope(granted_scopes: list[str], scope_type: str | None) -> bool:
    """Whether one of the granted scopes contains the integration's scope fragment; True when scope_type is missing
    or unknown (no filtering).
    """
    scope_fragment = SCOPE_FRAGMENT_BY_INTEGRATION.get(scope_type)
    if scope_fragment is None:
        return True
    return any(scope_fragment in granted_scope for granted_scope in granted_scopes)


@calendar_router.get("/connected/{tenant_name}")
async def list_connected_google_accounts(
    tenant_name: str, scope_type: Optional[str] = Query(None), current_user: User = Depends(authenticate_current_user)
):
    """Get GoogleShared connections filtered by scope type (calendar or email).
    Returns list of {email, scopes, default} for each connection.
    If scope_type is provided, filters by that scope in the scopes list.
    """
    connected_accounts = GoogleShared.objects(tenant_name=tenant_name)
    account_summaries = []
    for account_record in connected_accounts:
        granted_scopes = account_record.scopes or []
        if not _has_integration_scope(granted_scopes, scope_type):
            continue
        account_summaries.append(
            {"email": account_record.email, "default": account_record.default, "scopes": granted_scopes}
        )
    return JSONResponse(content=account_summaries)


def _stop_gmail_watch_for_account(account_record: GoogleShared) -> None:
    """Ask Gmail to stop pushing notifications for this account; errors are only logged."""
    try:
        gmail_client = build_gmail_client(account_record.access_token, account_record.refresh_token)
        gmail_client.users().stop(userId="me").execute()
    except Exception as error:
        logger.error(f"[disconnect_google] Error stopping gmail watch for {account_record.email}: {error}")


def _strip_integration_scopes(granted_scopes: list[str], scope_type: str | None) -> list[str]:
    """The scopes left once the integration's ones are removed; [] when no scope_type is given (the whole account
    goes).
    """
    if scope_type:
        scope_fragment = SCOPE_FRAGMENT_BY_INTEGRATION[scope_type]
        return [granted_scope for granted_scope in granted_scopes if scope_fragment not in granted_scope]
    return []


def _apply_scope_removal(account_record: GoogleShared, remaining_scopes: list[str], scope_type: str | None) -> bool:
    """Save the record with the remaining scopes (clearing the default flag for calendar, the Gmail watch flag for
    email), or delete it when no scope is left; returns whether the record was deleted.
    """
    if remaining_scopes:
        account_record.scopes = remaining_scopes
        if scope_type == "calendar":
            account_record.default = False
        if scope_type == "email":
            account_record.gmail_watch_enabled = False
        account_record.save()
        return False
    account_record.delete()
    return True


@calendar_router.delete("/disconnect/{tenant_name}/{email}")
async def disconnect_google_integration(
    email: str,
    tenant_name: Annotated[str, Depends(resolve_existing_tenant_name)],
    current_user: Annotated[User, Depends(authenticate_current_user)],
    scope_type: Annotated[
        Optional[str], Query(description="calendar | email | meet — omit to remove the whole account")
    ] = None,
):
    """Disconnect one Google integration (Calendar, Gmail or Meet) for a shared account.

    A single GoogleShared record backs all three cards, so removing one integration only
    strips its scopes; the record itself is deleted once no scope is left (or when
    `scope_type` is omitted, which disconnects the account entirely).
    """
    account_record = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    if not account_record:
        return JSONResponse(content={"message": "No connected Google account found for this email"}, status_code=404)

    if scope_type and scope_type not in SCOPE_FRAGMENT_BY_INTEGRATION:
        return JSONResponse(content={"message": f"Unknown scope_type '{scope_type}'"}, status_code=400)

    granted_scopes = list(account_record.scopes or [])
    remaining_scopes = _strip_integration_scopes(granted_scopes, scope_type)

    if scope_type in (None, "email"):
        _stop_gmail_watch_for_account(account_record)

    is_account_deleted = _apply_scope_removal(account_record, remaining_scopes, scope_type)

    return JSONResponse(
        content={"message": "Google integration disconnected", "email": email, "account_deleted": is_account_deleted}
    )


class UpdateSharedCalendar(BaseModel):
    """Body of PATCH /update: default flag, opening hours and the ignore-long-events setting of a calendar."""
    default: Optional[bool] = None
    opening_hours: Optional[List[OpeningHoursModel]] = None
    ignore_long_events: Optional[bool] = None


@calendar_router.patch("/update/{tenant_name}/{email}")
async def update_calendar_settings(
    calendar_patch: UpdateSharedCalendar,
    email: Optional[str] = None,
    tenant_name: str = Depends(resolve_existing_tenant_name),
    current_user: User = Depends(authenticate_current_user),
):
    """Update a connected calendar: default flag (only one default per tenant), opening hours, ignore_long_events."""
    email = email or current_user.email
    calendar_record = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    if not calendar_record:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    calendar_record.default = calendar_patch.default if calendar_patch.default is not None else calendar_record.default
    if calendar_patch.ignore_long_events is not None:
        calendar_record.ignore_long_events = calendar_patch.ignore_long_events
    if calendar_patch.opening_hours:
        calendar_record.opening_hours = [
            OpeningHours(**day_hours.model_dump()) for day_hours in calendar_patch.opening_hours
        ]
    calendar_record.save()
    calendar_record.reload()
    if calendar_patch.default:
        GoogleShared.objects(tenant_name=tenant_name, email__ne=email).update(default=False)

    return JSONResponse(content={"message": "calendar updated"})


#  ########################### Event API ############################
def _find_calendar_record(tenant_name: str, email: str | None, current_user: User) -> GoogleShared | None:
    """The requested email's record (no fallback when an email is given), else the dashboard user's, else the
    tenant's first one.
    """
    if email:
        return GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    calendar_record = GoogleShared.objects(tenant_name=tenant_name, email=current_user.email).first()
    if not calendar_record:
        calendar_record = GoogleShared.objects(tenant_name=tenant_name).first()
    return calendar_record


def _build_default_event_window() -> tuple[str, str]:
    """January 1st of last year to January 1st of next year, as ISO strings with a 'Z' suffix."""
    current_year = datetime.now().year
    last_year_start = datetime(current_year - 1, 1, 1)
    window_start_iso = last_year_start.isoformat() + "Z"
    window_end_iso = datetime(current_year + 1, 1, 1).isoformat() + "Z"
    return window_start_iso, window_end_iso


def _fetch_all_event_pages(
    calendar_client, google_calendar_id: str, time_min: str | None, time_max: str | None
) -> list[dict]:
    """Every event of the calendar between time_min and time_max, following nextPageToken; Google errors propagate."""
    # Fetch the events from the calendar
    google_events = []
    page_token = None

    while True:
        events_page = (
            calendar_client.events()
            .list(
                calendarId=google_calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                showDeleted=False,
                pageToken=page_token,
            )
            .execute()
        )

        page_events = events_page.get("items", [])
        google_events.extend(page_events)

        page_token = events_page.get("nextPageToken")
        if not page_token:
            break

    return google_events


def _format_events_for_dashboard(google_events: list[dict]) -> list[dict]:
    """The Google events in the dashboard calendar's shape; events without a start or an end are left out."""
    # Format the events for react-big-calendar
    return [
        {
            "title": event.get("summary", "No Title"),
            "start": event["start"].get("dateTime") or event["start"].get("date"),
            "end": event["end"].get("dateTime") or event["end"].get("date"),
            "id": event.get("id"),
            "location": event.get("location"),
            "guests": event.get("attendees"),
            "description": event.get("description"),
            "hangoutLink": event.get("hangoutLink"),
        }
        for event in google_events
        if "start" in event and "end" in event
    ]


@calendar_router.get("/events/{tenant_name}")
async def fetch_calendar_events(
    tenant_name: str,
    current_user: User = Depends(authenticate_current_user),
    email: Optional[str] = Query(None),
    timeMin: Optional[str] = Query(default=None),
    timeMax: Optional[str] = Query(default=None),
    maxResults: Optional[int] = Query(default=None),
):
    """Events of a connected calendar (the requested email, else the user's, else the first one) between timeMin and
    timeMax, formatted for the dashboard calendar; defaults to last year through next year.
    """
    calendar_record = _find_calendar_record(tenant_name, email, current_user)
    if not calendar_record:
        return JSONResponse(content=[])
    calendar_client = build_calendar_client(calendar_record.access_token, calendar_record.refresh_token)
    if not timeMin:
        timeMin, timeMax = _build_default_event_window()

    google_events = _fetch_all_event_pages(calendar_client, calendar_record.email, timeMin, timeMax)
    dashboard_events = _format_events_for_dashboard(google_events)

    return CustomJSONResponse(content=dashboard_events)


class EventModel(BaseModel):
    """Body of the event create / update endpoints."""
    start: str
    end: str
    summary: str
    location: Optional[str] = None
    description: Optional[str] = None
    guests: list = []
    add_meet_link: Optional[bool] = False


def _find_calendar_record_or_default(tenant_name: str, email: str | None, current_user: User) -> GoogleShared | None:
    """The requested email's record or the dashboard user's; when it is missing (even for a requested email), the
    tenant's default calendar, else its first one.
    """
    if email:
        calendar_record = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    else:
        calendar_record = GoogleShared.objects(tenant_name=tenant_name, email=current_user.email).first()
    if not calendar_record:
        calendar_record = (
            GoogleShared.objects(tenant_name=tenant_name, default=True).first()
            or GoogleShared.objects(tenant_name=tenant_name).first()
        )
    return calendar_record


def _compose_calendar_event_body(requested_event: EventModel, calendar_timezone_name: str | None) -> dict:
    """Google event body of the dashboard's event in the calendar's timezone, with a new Meet conference when
    add_meet_link is set.
    """
    event_body = {
        "summary": requested_event.summary,
        "location": requested_event.location,
        "description": requested_event.description,
        "start": {"dateTime": requested_event.start, "timeZone": calendar_timezone_name},
        "end": {"dateTime": requested_event.end, "timeZone": calendar_timezone_name},
        "attendees": requested_event.guests,
    }
    if requested_event.add_meet_link:
        event_body["conferenceData"] = {
            "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        }
    return event_body


def _write_event(calendar_client, google_calendar_id: str, event_body: dict, event_id: str | None) -> dict:
    """Update the Google event when event_id is given, else insert a new one (both with conferenceDataVersion=1);
    returns Google's event resource.
    """
    if event_id:
        return (
            calendar_client.events()
            .update(calendarId=google_calendar_id, eventId=event_id, body=event_body, conferenceDataVersion=1)
            .execute()
        )
    return (
        calendar_client.events()
        .insert(calendarId=google_calendar_id, body=event_body, conferenceDataVersion=1)
        .execute()
    )


@calendar_router.patch("/events/{tenant_name}/{event_id}")
@calendar_router.post("/events/{tenant_name}")
async def create_or_update_calendar_event(
    tenant_name: str,
    requested_event: EventModel,
    event_id: Optional[str] = None,
    current_user: User = Depends(authenticate_current_user),
    email: Optional[str] = Query(None),
):
    """Create (POST) or update (PATCH with event_id) an event in the tenant's calendar, in the calendar's own
    timezone; a Meet link can be added.
    """
    calendar_record = _find_calendar_record_or_default(tenant_name, email, current_user)
    if not calendar_record:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    calendar_client = build_calendar_client(calendar_record.access_token, calendar_record.refresh_token)
    calendar_timezone_name = (
        calendar_client.calendarList().get(calendarId=calendar_record.email).execute().get("timeZone")
    )
    event_body = _compose_calendar_event_body(requested_event, calendar_timezone_name)
    saved_event = _write_event(calendar_client, calendar_record.email, event_body, event_id)
    return CustomJSONResponse(content=saved_event)


@calendar_router.delete("/events/{tenant_name}/{event_id}")
async def delete_calendar_event(
    tenant_name: str, event_id: str, current_user: User = Depends(authenticate_current_user), email: Optional[str] = Query(None)
):
    """Delete an event from the tenant's calendar (the requested email, else the user's, else the first connected
    one).
    """
    calendar_record = _find_calendar_record(tenant_name, email, current_user)
    if not calendar_record:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    calendar_client = build_calendar_client(calendar_record.access_token, calendar_record.refresh_token)
    deletion_result = calendar_client.events().delete(calendarId=calendar_record.email, eventId=event_id).execute()
    return CustomJSONResponse(content=deletion_result)
