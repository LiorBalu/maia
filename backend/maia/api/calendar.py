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

from maia.services.registry import get_tenant_name
from maia.models.users import User
from maia.models.google_accounts import GoogleShared, OpeningHours, OpeningHoursModel
from maia.api.security import authenticate_user_token
from maia.utils.CustomJSONResponse import CustomJSONResponse
from maia.api.email import RegisterEmail, get_email_service, register_email

logger = logging.getLogger(__name__)

GOOGLE_CLIENT_ID = getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = getenv("GOOGLE_CLIENT_SECRET")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
# The consent flow asks Google for incremental authorization, so the token returned when an account
# connects Gmail also covers the calendar scopes it granted earlier (and vice versa). oauthlib
# raises on that "scope changed" response unless told to accept it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


calendar_router = APIRouter(prefix="/calendar", tags=["Calendar"])


BACKEND = getenv("BACKEND_URL", "http://localhost:8000")

REDIRECT_URI = f"{BACKEND}/management/v1/calendar/oauth2callback"

flow = Flow.from_client_config(
    {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    },
    scopes=[
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.labels",
        "https://www.googleapis.com/auth/gmail.modify",
    ],
    redirect_uri=REDIRECT_URI,
)

scopes_map = {
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


def get_calendar_service(access_token: str, refresh_token: str):
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds)


def is_time_slot_available(service, calendar_id, start_time, end_time, timezone, ignore_long_events=False):
    """
    Check if a specific time slot is available.
    Uses Events List API (same as get_available_time_slots_in_range) for consistency.
    FreeBusy API can include events from subscribed/shared calendars which causes mismatches.
    """

    logger.info(f"\n{'=' * 60}")
    logger.info("[is_time_slot_available] CALLED")
    logger.info(f"[is_time_slot_available] calendar_id: {calendar_id}")
    logger.info(f"[is_time_slot_available] start_time: {start_time}")
    logger.info(f"[is_time_slot_available] end_time: {end_time}")
    logger.info(f"[is_time_slot_available] timezone: {timezone}")
    logger.info(f"[is_time_slot_available] ignore_long_events: {ignore_long_events}")

    # Use Events List API instead of FreeBusy for consistency with get_available_time_slots_in_range
    # FreeBusy can include events from subscribed/shared calendars causing mismatches
    logger.info("[is_time_slot_available] Using Events List API for consistency...")

    try:
        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start_time,
                timeMax=end_time,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,  # Small range, shouldn't have many events
            )
            .execute()
        )

        events = events_result.get("items", [])
        logger.info(f"[is_time_slot_available] Events List API returned {len(events)} events")

    except Exception as e:
        logger.error(f"[is_time_slot_available] ERROR calling Events List API: {e}")
        # Fallback: assume NOT available if API fails (safer - prevents double bookings)
        logger.error("[is_time_slot_available] Fallback -> NOT AVAILABLE (False) - to prevent double bookings")
        logger.error(f"{'=' * 60}\n")
        return False

    # Parse the requested time slot for overlap checking
    slot_start = datetime.fromisoformat(start_time)
    slot_end = datetime.fromisoformat(end_time)
    tz = pytz.timezone(timezone)

    # Check each event for overlap with the requested slot
    blocking_events = []

    for event in events:
        # Skip cancelled events
        if event.get("status") == "cancelled":
            logger.warning(f"[is_time_slot_available]   Skipping cancelled event: {event.get('summary', 'No title')}")
            continue

        # Skip transparent events (marked as "free" / "available")
        if event.get("transparency") == "transparent":
            logger.warning(f"[is_time_slot_available]   Skipping transparent (free) event: {event.get('summary', 'No title')}")
            continue

        # Get start and end times
        start = event.get("start", {})
        end = event.get("end", {})

        # Handle all-day events (have 'date' instead of 'dateTime')
        if "date" in start:
            event_start_str = start["date"] + "T00:00:00"
            event_end_str = end["date"] + "T00:00:00"
            event_start = tz.localize(datetime.fromisoformat(event_start_str))
            event_end = tz.localize(datetime.fromisoformat(event_end_str))
        else:
            event_start = datetime.fromisoformat(start.get("dateTime"))
            event_end = datetime.fromisoformat(end.get("dateTime"))

        # Calculate duration
        duration_hours = (event_end - event_start).total_seconds() / 3600
        event_summary = event.get("summary", "No title")

        logger.info(f"[is_time_slot_available]   Event: '{event_summary}' | {event_start} to {event_end} ({duration_hours:.1f}h)")

        # Filter out long events (> 12 hours) if the setting is enabled
        if ignore_long_events and duration_hours > 12:
            logger.info("[is_time_slot_available]     -> FILTERED (>12h, ignore_long_events=True)")
            continue

        # Check if event overlaps with the requested slot
        # Overlap occurs if: event_start < slot_end AND event_end > slot_start
        if event_start < slot_end and event_end > slot_start:
            logger.info("[is_time_slot_available]     -> BLOCKS the requested slot")
            blocking_events.append(event_summary)
        else:
            logger.info("[is_time_slot_available]     -> Does not block (no overlap)")

    logger.info(f"[is_time_slot_available] Total blocking events: {len(blocking_events)}")

    if not blocking_events:
        logger.info("[is_time_slot_available] No blocking events -> AVAILABLE (True)")
        logger.info(f"{'=' * 60}\n")
        return True

    logger.info(f"[is_time_slot_available] Has {len(blocking_events)} blocking events -> NOT AVAILABLE (False)")
    logger.info(f"[is_time_slot_available] Blocking events: {blocking_events}")
    logger.info(f"{'=' * 60}\n")
    return False


def get_available_time_slots_in_range(
    service, calendar_id, start_date, end_date, timezone, slot_duration_minutes=30, ignore_long_events=False
):
    """
    Retrieve available time slots within a range of dates.
    Each slot has a duration of slot_duration_minutes.
    If ignore_long_events is True, events longer than 12 hours are ignored.

    IMPORTANT: We use the Events List API instead of FreeBusy to get ACTUAL events,
    not merged busy blocks. FreeBusy merges events when all-day events exist.
    """

    logger.info(f"\n{'=' * 60}")
    logger.info("[get_available_time_slots_in_range] CALLED")
    logger.info(f"[get_available_time_slots_in_range] calendar_id: {calendar_id}")
    logger.info(f"[get_available_time_slots_in_range] start_date: {start_date}")
    logger.info(f"[get_available_time_slots_in_range] end_date: {end_date}")
    logger.info(f"[get_available_time_slots_in_range] timezone: {timezone}")
    logger.info(f"[get_available_time_slots_in_range] slot_duration_minutes: {slot_duration_minutes}")
    logger.info(f"[get_available_time_slots_in_range] ignore_long_events: {ignore_long_events}")

    # Use Events List API instead of FreeBusy to get actual events (not merged blocks)
    logger.info("[get_available_time_slots_in_range] Using Events List API to get actual events...")

    events = []
    page_token = None

    try:
        # Handle pagination - events().list() may return multiple pages
        while True:
            events_result = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=start_date,
                    timeMax=end_date,
                    singleEvents=True,  # Expand recurring events into individual instances
                    orderBy="startTime",
                    pageToken=page_token,
                    maxResults=250,  # Max allowed per page
                )
                .execute()
            )

            events.extend(events_result.get("items", []))
            page_token = events_result.get("nextPageToken")

            if not page_token:
                break

        logger.info(f"[get_available_time_slots_in_range] Events List API returned {len(events)} events")

    except Exception as e:
        logger.error(f"[get_available_time_slots_in_range] ERROR calling Events List API: {e}")
        events = []

    # Convert events to busy slots format
    all_busy_slots = []
    tz = pytz.timezone(timezone)

    for event in events:
        # Skip cancelled events
        if event.get("status") == "cancelled":
            logger.warning(f"[get_available_time_slots_in_range]   Skipping cancelled event: {event.get('summary', 'No title')}")
            continue

        # Skip transparent events (marked as "free" / "available")
        # transparency: 'opaque' = busy (default), 'transparent' = free
        if event.get("transparency") == "transparent":
            logger.warning(f"[get_available_time_slots_in_range]   Skipping transparent (free) event: {event.get('summary', 'No title')}")
            continue

        # Get start and end times
        start = event.get("start", {})
        end = event.get("end", {})

        # Handle all-day events (have 'date' instead of 'dateTime')
        if "date" in start:
            # All-day event - date format is YYYY-MM-DD
            event_start_str = start["date"] + "T00:00:00"
            event_end_str = end["date"] + "T00:00:00"

            # Parse and add timezone (all-day events don't have timezone info)
            event_start = tz.localize(datetime.fromisoformat(event_start_str))
            event_end = tz.localize(datetime.fromisoformat(event_end_str))
        else:
            # Regular event with specific time - has timezone in dateTime string
            event_start = datetime.fromisoformat(start.get("dateTime"))
            event_end = datetime.fromisoformat(end.get("dateTime"))

        # Calculate duration
        duration_hours = (event_end - event_start).total_seconds() / 3600
        event_summary = event.get("summary", "No title")

        logger.info(f"[get_available_time_slots_in_range]   Event: '{event_summary}' | {event_start} to {event_end} ({duration_hours:.1f}h)")

        # Filter out long events (> 12 hours) if the setting is enabled
        if ignore_long_events and duration_hours > 12:
            logger.info("[get_available_time_slots_in_range]     -> FILTERED (>12h, ignore_long_events=True)")
            continue

        logger.info("[get_available_time_slots_in_range]     -> KEPT as busy slot")

        all_busy_slots.append({"start": event_start.isoformat(), "end": event_end.isoformat()})

    logger.info(f"[get_available_time_slots_in_range] Total busy slots after filtering: {len(all_busy_slots)}")

    # Sort busy_slots by start time - algorithm requires sorted slots!
    busy_slots = sorted(all_busy_slots, key=lambda x: x["start"])
    logger.info(f"[get_available_time_slots_in_range] busy_slots AFTER sort: {busy_slots}")

    # List to store available slots
    available_slots = []

    # Initialize current_time to the start_date
    current_time = datetime.fromisoformat(start_date)

    logger.info("[get_available_time_slots_in_range] Starting slot generation...")
    logger.info(f"[get_available_time_slots_in_range] current_time initialized to: {current_time}")

    # Iterate through the busy slots and find gaps
    for i, busy_slot in enumerate(busy_slots):
        busy_start = datetime.fromisoformat(busy_slot["start"])
        busy_end = datetime.fromisoformat(busy_slot["end"])

        logger.info(f"[get_available_time_slots_in_range] Processing busy_slot[{i}]: {busy_start} to {busy_end}")
        logger.info(f"[get_available_time_slots_in_range]   current_time before loop: {current_time}")

        slots_added = 0
        # Check if there's a gap between current_time and the busy start time
        while current_time + timedelta(minutes=slot_duration_minutes) <= busy_start:
            available_slots.append((current_time, current_time + timedelta(minutes=slot_duration_minutes)))
            slots_added += 1
            current_time += timedelta(minutes=slot_duration_minutes)

        logger.info(f"[get_available_time_slots_in_range]   Added {slots_added} available slots before this busy slot")

        # Update current_time to be after the busy slot
        old_current = current_time
        current_time = max(current_time, busy_end)
        logger.info(f"[get_available_time_slots_in_range]   current_time updated: {old_current} -> {current_time}")

    logger.info(f"[get_available_time_slots_in_range] After processing all busy slots, current_time: {current_time}")

    # After the last busy slot, check for available slots until the end_date
    slots_added_after = 0
    while current_time + timedelta(minutes=slot_duration_minutes) <= datetime.fromisoformat(end_date):
        available_slots.append((current_time, current_time + timedelta(minutes=slot_duration_minutes)))
        slots_added_after += 1
        current_time += timedelta(minutes=slot_duration_minutes)

    logger.info(f"[get_available_time_slots_in_range] Added {slots_added_after} slots after last busy slot")
    logger.info(f"[get_available_time_slots_in_range] Total available_slots generated: {len(available_slots)}")
    logger.info(f"[get_available_time_slots_in_range] First 10 slots: {available_slots[:10]}")
    logger.info(f"{'=' * 60}\n")

    return available_slots


@calendar_router.get("/authorization-url/{tenant_name}")
async def google_calendar_register(
    tenant_name: str,
    request: Request,
    scopes: Optional[str] = Query(""),
    pub_sub: bool = Query(False),
    user: User = Depends(authenticate_user_token),
):
    state = json.dumps({"tenant_name": tenant_name, "scopes": scopes, "pub_sub": pub_sub})

    scopes_list = scopes.split(",")
    selected_scopes = []

    for scope_key in scopes_list:
        key = scope_key.strip().lower()
        if key in scopes_map:
            selected_scopes.extend(scopes_map[key])
    selected_scopes = list(set(selected_scopes))
    if "openid" not in selected_scopes:
        selected_scopes.append("openid")

    if not selected_scopes:
        return JSONResponse(status_code=400, content={"error": "No valid scopes provided."})

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=selected_scopes,
        redirect_uri=REDIRECT_URI,
    )

    authorization_url, state = flow.authorization_url(
        prompt="consent", state=state, access_type="offline", include_granted_scopes="true"
    )
    # Store the code_verifier in the state so we can use it in the callback (required for PKCE)
    state_data_with_verifier = json.loads(state)
    state_data_with_verifier["code_verifier"] = flow.code_verifier
    updated_state = json.dumps(state_data_with_verifier)
    # Replace the state parameter in the authorization URL
    parsed = urlparse(authorization_url)
    qs = parse_qs(parsed.query)
    qs["state"] = [updated_state]
    new_query = urlencode(qs, doseq=True)
    authorization_url = urlunparse(parsed._replace(query=new_query))
    return JSONResponse(content={"authorization_url": authorization_url})


def _merge_scopes(existing: list[str] | None, granted: list[str]) -> list[str]:
    """Union of the scopes stored on a GoogleShared record and the ones granted in this consent."""
    return sorted(set(existing or []) | set(granted))


def _oauth_popup_html(payload, body_text: str) -> HTMLResponse:
    """Render a popup HTML that posts `payload` to opener and closes itself."""
    payload_json = json.dumps(payload)
    html_content = f"""
    <html>
        <head>
            <script>
                window.onload = function() {{
                    if (window.opener) {{
                        window.opener.postMessage({payload_json}, '*');
                    }}
                    window.close();
                }};
            </script>
        </head>
        <body>
            <p>{body_text}</p>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


def _oauth_popup_success(warning: str = "") -> HTMLResponse:
    """Success popup. When `warning` is provided, sends a structured payload so the
    front can show both a success toast and a warning toast (partial success)."""
    if warning:
        payload = {"type": "auth_success", "warning": warning}
        body = warning
    else:
        payload = "auth_success"
        body = "Authentication successful! You can close this window."
    return _oauth_popup_html(payload, body)


def _oauth_popup_error(message: str) -> HTMLResponse:
    """Error popup. Sends `{type:'auth_error', message}` so the front displays it."""
    return _oauth_popup_html({"type": "auth_error", "message": message}, message)


@calendar_router.get("/oauth2callback")
async def oauth2callback(request: Request):
    query_params = request.query_params
    state = query_params.get("state")
    if not state:
        return JSONResponse(status_code=400, content={"error": "State is missing."})

    state_data = json.loads(state)

    tenant_name = state_data.get("tenant_name")
    scopes = state_data.get("scopes", "")
    pub_sub = state_data.get("pub_sub", False)
    scopes_list = scopes.split(",")
    if not tenant_name:
        return JSONResponse(status_code=400, content={"error": "Tenant name is missing."})
    logger.info(" ".join(str(part) for part in ("Headers: ", request.headers)))
    logger.info(" ".join(str(part) for part in ("URL: ", request.url)))
    selected_scopes = []
    for scope_key in scopes_list:
        key = scope_key.strip().lower()
        if key in scopes_map:
            selected_scopes.extend(scopes_map[key])
    selected_scopes = list(set(selected_scopes))
    if "openid" not in selected_scopes:
        selected_scopes.append("openid")
    if not selected_scopes:
        return JSONResponse(status_code=400, content={"error": "No valid scopes provided."})

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=selected_scopes,
        redirect_uri=REDIRECT_URI,
    )
    # Restore the PKCE code_verifier from state
    code_verifier = state_data.get("code_verifier")
    if code_verifier:
        flow.code_verifier = code_verifier
    # Extract the authorization code directly from query params
    # (avoids URL host mismatch issues with proxies/tunnels)
    authorization_code = request.query_params.get("code")
    if not authorization_code:
        return JSONResponse(status_code=400, content={"error": "Authorization code is missing."})
    flow.fetch_token(code=authorization_code)
    credentials = flow.credentials
    logger.info(" ".join(str(part) for part in ("credential: ", credentials)))
    oauth2_service = build("oauth2", "v2", credentials=credentials)
    user_info = oauth2_service.userinfo().get().execute()
    user_email = user_info.get("email")

    if "calendar" in scopes_list:
        # Build calendar service and fetch ALL user calendars
        service = build("calendar", "v3", credentials=credentials)
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get("items", [])

        # Identify the primary calendar email (used as default)
        primary_calendar_email = user_email
        for cal in calendars:
            if cal.get("primary") is True:
                primary_calendar_email = cal["id"]
                break

        logger.info(f"[oauth2callback] Found {len(calendars)} calendars. Primary: {primary_calendar_email}")

        saved_or_updated: list[str] = []
        skipped_taken_by_other_tenant: list[str] = []

        # Save each calendar as a separate GoogleShared entry
        for cal in calendars:
            cal_email = cal["id"]
            is_default = cal_email == primary_calendar_email

            # One record per account and tenant (`email` is unique across the collection): when the
            # account was connected for Gmail first, merge the calendar scopes into that record.
            existing = GoogleShared.objects(tenant_name=tenant_name, email=cal_email).first()

            if existing:
                # Update credentials and merge scopes on the existing entry
                existing.update(
                    access_token=credentials.token,
                    refresh_token=credentials.refresh_token or existing.refresh_token,
                    expires_at=credentials.expiry,
                    scopes=_merge_scopes(existing.scopes, selected_scopes),
                    default=is_default,
                )
                existing.reload()
                saved_or_updated.append(cal_email)
                logger.info(f"[oauth2callback] Updated calendar: {cal_email} (default={is_default})")
            else:
                try:
                    GoogleShared(
                        tenant_name=tenant_name,
                        email=cal_email,
                        access_token=credentials.token,
                        refresh_token=credentials.refresh_token,
                        expires_at=credentials.expiry,
                        scopes=selected_scopes,
                        default=is_default,
                    ).save()
                    saved_or_updated.append(cal_email)
                    logger.info(f"[oauth2callback] Created calendar: {cal_email} (default={is_default})")
                except NotUniqueError:
                    # Calendar email is already linked to another tenant — skip it and continue
                    skipped_taken_by_other_tenant.append(cal_email)
                    logger.error(f"[oauth2callback] Skipped {cal_email}: already connected to another tenant")

        # Ensure only the primary has default=True among calendar entries for this tenant
        # (only when the primary was actually saved/updated, otherwise this would clear defaults wrongly)
        if primary_calendar_email in saved_or_updated:
            GoogleShared.objects(tenant_name=tenant_name, email__ne=primary_calendar_email).filter(
                scopes__contains="https://www.googleapis.com/auth/calendar"
            ).update(default=False)

        # Decide the popup response based on partial / full / total failure
        if not saved_or_updated and skipped_taken_by_other_tenant:
            return _oauth_popup_error(
                "All selected calendars are already connected to another tenant: "
                + ", ".join(skipped_taken_by_other_tenant)
            )
        if skipped_taken_by_other_tenant:
            return _oauth_popup_success(
                warning=(
                    "Some calendars were skipped because they are already connected to "
                    "another tenant: " + ", ".join(skipped_taken_by_other_tenant)
                )
            )
        return _oauth_popup_success()


    elif "email" in scopes_list:
        # For email scope, save a single entry for the user's email
        primary_email = user_email

        # One record per account and tenant (`email` is unique across the collection): when the
        # account was connected for the calendar first, merge the Gmail scopes into that record.
        existing_email_entry = GoogleShared.objects(tenant_name=tenant_name, email=primary_email).first()

        if existing_email_entry:
            existing_email_entry.update(
                access_token=credentials.token,
                refresh_token=credentials.refresh_token or existing_email_entry.refresh_token,
                expires_at=credentials.expiry,
                scopes=_merge_scopes(existing_email_entry.scopes, selected_scopes),
            )
            existing_email_entry.reload()
            logger.info(f"[oauth2callback] Updated email entry: {primary_email}")
        else:
            try:
                GoogleShared(
                    tenant_name=tenant_name,
                    email=primary_email,
                    access_token=credentials.token,
                    refresh_token=credentials.refresh_token,
                    expires_at=credentials.expiry,
                    scopes=selected_scopes,
                ).save()
                logger.info(f"[oauth2callback] Created email entry: {primary_email}")
            except NotUniqueError:
                return _oauth_popup_error(f"Email {primary_email} is already connected to another tenant.")

        # Gmail watch (Pub/Sub push) if requested. Runs after the record is saved so the registration
        # finds this account's tokens. It needs a public HTTPS BACKEND_URL; when it cannot be set up
        # the account stays connected and the popup reports that receiving mail is not active yet.
        if pub_sub:
            try:
                register_email_data = RegisterEmail(email=primary_email)

                class MockUser:
                    def __init__(self, tenant_name):
                        self.tenants_name = [tenant_name]

                mock_user = MockUser(tenant_name)
                response = await register_email(
                    request=request, register_email=register_email_data, tenant_name=tenant_name, user=mock_user
                )
                if getattr(response, "status_code", 200) >= 400:
                    body = json.loads(response.body)
                    detail = body.get("error") or body.get("message") or ""
                    logger.info(f"[oauth2callback] Gmail watch not set up for {primary_email}: {detail}")
                    return _oauth_popup_success(warning=f"Receiving mail is not active yet: {detail}")
                logger.info(f"[oauth2callback] Gmail watch setup successful for {primary_email}")
            except Exception as e:
                logger.error(f"[oauth2callback] Error setting up Gmail watch: {e}")
                return _oauth_popup_success(warning=f"Receiving mail is not active yet: {e}")

    elif "meet" in scopes_list:
        # Save a single meet-scoped entry for the connected account (the meeting organizer).
        # The Meet poller reads this entry to pull conference records + artifacts.
        # `email` is globally unique on GoogleShared, so if this account is already connected
        # for the SAME tenant (e.g. calendar), merge the meet scopes into that entry instead of
        # creating a colliding doc.
        meet_email = user_email
        existing_entry = GoogleShared.objects(tenant_name=tenant_name, email=meet_email).first()
        if existing_entry:
            merged_scopes = list(set((existing_entry.scopes or []) + selected_scopes))
            existing_entry.update(
                access_token=credentials.token,
                refresh_token=credentials.refresh_token,
                expires_at=credentials.expiry,
                scopes=merged_scopes,
            )
            existing_entry.reload()
            logger.info(f"[oauth2callback] Merged meet scopes into existing entry: {meet_email}")
        else:
            try:
                GoogleShared(
                    tenant_name=tenant_name,
                    email=meet_email,
                    access_token=credentials.token,
                    refresh_token=credentials.refresh_token,
                    expires_at=credentials.expiry,
                    scopes=selected_scopes,
                ).save()
                logger.info(f"[oauth2callback] Created meet entry: {meet_email}")
            except NotUniqueError:
                return _oauth_popup_error(f"Account {meet_email} is already connected to another tenant.")
    return _oauth_popup_success()


@calendar_router.get("/all/{tenant_name}")
async def get_register_calendar(tenant_name: str, user: User = Depends(authenticate_user_token)):
    calendars = GoogleShared.objects(tenant_name=tenant_name)
    return CustomJSONResponse(content=[calendar.to_mongo().to_dict() for calendar in calendars])


@calendar_router.get("/connected/{tenant_name}")
async def get_connected_google_shared(
    tenant_name: str, scope_type: Optional[str] = Query(None), user: User = Depends(authenticate_user_token)
):
    """Get GoogleShared connections filtered by scope type (calendar or email).
    Returns list of {email, scopes, default} for each connection.
    If scope_type is provided, filters by that scope in the scopes list.
    """
    all_shared = GoogleShared.objects(tenant_name=tenant_name)
    results = []
    for gs in all_shared:
        gs_scopes = gs.scopes or []
        if scope_type == "calendar":
            if not any("calendar" in s for s in gs_scopes):
                continue
        elif scope_type == "email":
            if not any("gmail" in s for s in gs_scopes):
                continue
        elif scope_type == "meet":
            if not any("meetings" in s for s in gs_scopes):
                continue
        results.append({"email": gs.email, "default": gs.default, "scopes": gs_scopes})
    return JSONResponse(content=results)


# Scope fragment used by /connected to bucket a GoogleShared record per integration card
SCOPE_TYPE_MARKERS = {"calendar": "calendar", "email": "gmail", "meet": "meetings"}


def _stop_gmail_watch(shared: GoogleShared) -> None:
    try:
        service = get_email_service(shared.access_token, shared.refresh_token)
        service.users().stop(userId="me").execute()
    except Exception as e:
        logger.error(f"[disconnect_google] Error stopping gmail watch for {shared.email}: {e}")


@calendar_router.delete("/disconnect/{tenant_name}/{email}")
async def disconnect_google_shared(
    email: str,
    tenant_name: Annotated[str, Depends(get_tenant_name)],
    user: Annotated[User, Depends(authenticate_user_token)],
    scope_type: Annotated[
        Optional[str], Query(description="calendar | email | meet — omit to remove the whole account")
    ] = None,
):
    """Disconnect one Google integration (Calendar, Gmail or Meet) for a shared account.

    A single GoogleShared record backs all three cards, so removing one integration only
    strips its scopes; the record itself is deleted once no scope is left (or when
    `scope_type` is omitted, which disconnects the account entirely).
    """
    shared = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    if not shared:
        return JSONResponse(content={"message": "No connected Google account found for this email"}, status_code=404)

    if scope_type and scope_type not in SCOPE_TYPE_MARKERS:
        return JSONResponse(content={"message": f"Unknown scope_type '{scope_type}'"}, status_code=400)

    scopes = list(shared.scopes or [])
    if scope_type:
        marker = SCOPE_TYPE_MARKERS[scope_type]
        remaining_scopes = [s for s in scopes if marker not in s]
    else:
        remaining_scopes = []

    if scope_type in (None, "email"):
        _stop_gmail_watch(shared)

    if remaining_scopes:
        shared.scopes = remaining_scopes
        if scope_type == "calendar":
            shared.default = False
        if scope_type == "email":
            shared.gmail_watch_enabled = False
        shared.save()
        deleted = False
    else:
        shared.delete()
        deleted = True

    return JSONResponse(
        content={"message": "Google integration disconnected", "email": email, "account_deleted": deleted}
    )


class UpdateSharedCalendar(BaseModel):
    default: Optional[bool] = None
    opening_hours: Optional[List[OpeningHoursModel]] = None
    ignore_long_events: Optional[bool] = None


@calendar_router.patch("/update/{tenant_name}/{email}")
async def update_calendar_webhook(
    update: UpdateSharedCalendar,
    email: Optional[str] = None,
    tenant_name: str = Depends(get_tenant_name),
    user: User = Depends(authenticate_user_token),
):
    email = email or user.email
    calendar = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    if not calendar:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    calendar.default = update.default if update.default is not None else calendar.default
    if update.ignore_long_events is not None:
        calendar.ignore_long_events = update.ignore_long_events
    if update.opening_hours:
        calendar.opening_hours = [OpeningHours(**opening_hours.model_dump()) for opening_hours in update.opening_hours]
    calendar.save()
    calendar.reload()
    if update.default:
        GoogleShared.objects(tenant_name=tenant_name, email__ne=email).update(default=False)

    return JSONResponse(content={"message": "calendar updated"})


#  ########################### Event API ############################
@calendar_router.get("/events/{tenant_name}")
async def get_events(
    tenant_name: str,
    user: User = Depends(authenticate_user_token),
    email: Optional[str] = Query(None),
    timeMin: Optional[str] = Query(default=None),
    timeMax: Optional[str] = Query(default=None),
    maxResults: Optional[int] = Query(default=None),
):
    if email:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
        if not calendar:
            return JSONResponse(content=[])
    else:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=user.email).first()
        if not calendar:
            calendar = GoogleShared.objects(tenant_name=tenant_name).first()
            if not calendar:
                return JSONResponse(content=[])
    service = get_calendar_service(calendar.access_token, calendar.refresh_token)
    if not timeMin:
        current_year = datetime.now().year
        last_year_date = datetime(current_year - 1, 1, 1)
        timeMin = last_year_date.isoformat() + "Z"
        timeMax = datetime(current_year + 1, 1, 1).isoformat() + "Z"

    # Fetch the events from the calendar
    raw_events = []
    page_token = None

    while True:
        events_result = (
            service.events()
            .list(
                calendarId=calendar.email,
                timeMin=timeMin,
                timeMax=timeMax,
                singleEvents=True,
                orderBy="startTime",
                showDeleted=False,
                pageToken=page_token,
            )
            .execute()
        )

        events = events_result.get("items", [])
        raw_events.extend(events)

        page_token = events_result.get("nextPageToken")
        if not page_token:
            break

    # Format the events for react-big-calendar
    formatted_events = [
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
        for event in raw_events
        if "start" in event and "end" in event
    ]

    return CustomJSONResponse(content=formatted_events)


class EventModel(BaseModel):
    start: str
    end: str
    summary: str
    location: Optional[str] = None
    description: Optional[str] = None
    guests: list = []
    add_meet_link: Optional[bool] = False


@calendar_router.patch("/events/{tenant_name}/{event_id}")
@calendar_router.post("/events/{tenant_name}")
async def create_or_patch_event(
    tenant_name: str,
    event: EventModel,
    event_id: Optional[str] = None,
    user: User = Depends(authenticate_user_token),
    email: Optional[str] = Query(None),
):
    calendar = None
    if email:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    else:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=user.email).first()
    if not calendar:
        calendar = (
            GoogleShared.objects(tenant_name=tenant_name, default=True).first()
            or GoogleShared.objects(tenant_name=tenant_name).first()
        )
    if not calendar:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    service = get_calendar_service(calendar.access_token, calendar.refresh_token)
    calendar_timezone = service.calendarList().get(calendarId=calendar.email).execute().get("timeZone")
    event_payload = {
        "summary": event.summary,
        "location": event.location,
        "description": event.description,
        "start": {"dateTime": event.start, "timeZone": calendar_timezone},
        "end": {"dateTime": event.end, "timeZone": calendar_timezone},
        "attendees": event.guests,
    }
    if event.add_meet_link:
        event_payload["conferenceData"] = {
            "createRequest": {"requestId": str(uuid.uuid4()), "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        }
    if event_id:
        event = (
            service.events()
            .update(calendarId=calendar.email, eventId=event_id, body=event_payload, conferenceDataVersion=1)
            .execute()
        )
    else:
        event = (
            service.events().insert(calendarId=calendar.email, body=event_payload, conferenceDataVersion=1).execute()
        )
    return CustomJSONResponse(content=event)


@calendar_router.delete("/events/{tenant_name}/{event_id}")
async def delete_event(
    tenant_name: str, event_id: str, user: User = Depends(authenticate_user_token), email: Optional[str] = Query(None)
):
    if email:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=email).first()
    else:
        calendar = GoogleShared.objects(tenant_name=tenant_name, email=user.email).first()
        if not calendar:
            calendar = GoogleShared.objects(tenant_name=tenant_name).first()
    if not calendar:
        return JSONResponse(content={"message": "No calendar found for your account please share"}, status_code=404)
    service = get_calendar_service(calendar.access_token, calendar.refresh_token)
    event = service.events().delete(calendarId=calendar.email, eventId=event_id).execute()
    return CustomJSONResponse(content=event)
