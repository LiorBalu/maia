"""Read-only access to a tenant's Google Sheet through the Maia service account.

The business shares its spreadsheet (for example an inventory list: product, SKU, price, stock)
with the service account in ``serviceKey.json``; the bot then looks rows up in real time with the
``st_search_sheet`` tool, and the dashboard shows a preview on the connection screen.

Only the first tab is read. The first row is the header; every other non-empty row becomes a
``{header: cell}`` dict. Reads are cached for a minute so a conversation with several questions
does not hit the Sheets API on every turn.
"""

import json
import re
import time
from dataclasses import dataclass

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SERVICE_KEY_PATH = "serviceKey.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
CACHE_TTL_SECONDS = 60

_SHEET_ID_IN_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_SHEET_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class SheetAccessError(Exception):
    """The spreadsheet does not exist or is not shared with the service account."""


@dataclass
class SheetData:
    title: str
    headers: list[str]
    rows: list[dict[str, str]]


_cache: dict[str, tuple[float, SheetData]] = {}


def service_account_email() -> str:
    """The address the business has to share its sheet with."""
    with open(SERVICE_KEY_PATH, encoding="utf-8") as fh:
        return json.load(fh).get("client_email", "")


def extract_sheet_id(url_or_id: str) -> str | None:
    """Spreadsheet id from a docs.google.com link, or the bare id itself; None when neither."""
    text = (url_or_id or "").strip()
    match = _SHEET_ID_IN_URL.search(text)
    if match:
        return match.group(1)
    return text if _BARE_SHEET_ID.match(text) else None


def _sheets_service():
    credentials = service_account.Credentials.from_service_account_file(SERVICE_KEY_PATH, scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_sheet(sheet_id: str, use_cache: bool = True) -> SheetData:
    """Title, headers and rows of the first tab. Raises SheetAccessError when Google refuses."""
    cached = _cache.get(sheet_id)
    if use_cache and cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    service = _sheets_service()
    try:
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="properties.title,sheets.properties.title")
            .execute()
        )
        first_tab = meta["sheets"][0]["properties"]["title"]
        values = (
            service.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{first_tab}'").execute()
        ).get("values", [])
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            raise SheetAccessError(
                f"The sheet is not reachable (HTTP {exc.resp.status}). "
                f"Share it with {service_account_email()} (Viewer is enough) and check the link."
            ) from exc
        raise

    headers = [str(cell).strip() for cell in (values[0] if values else [])]
    rows: list[dict[str, str]] = []
    for raw in values[1:]:
        cells = [str(cell).strip() for cell in raw]
        if not any(cells):
            continue
        cells += [""] * (len(headers) - len(cells))
        rows.append({header: cells[i] for i, header in enumerate(headers) if header})

    data = SheetData(title=meta["properties"]["title"], headers=[h for h in headers if h], rows=rows)
    _cache[sheet_id] = (time.time(), data)
    return data


def search_rows(rows: list[dict[str, str]], query: str, limit: int = 5) -> list[dict[str, str]]:
    """Rows whose cells contain the query words (case-insensitive); rows matching every word first."""
    tokens = [token for token in re.split(r"\s+", (query or "").lower().strip()) if token]
    if len(tokens) > 1:
        # "PlayStation 5": a single character matches half of the sheet, so it only adds noise
        tokens = [token for token in tokens if len(token) > 1] or tokens
    if not tokens:
        return []
    scored = []
    for row in rows:
        haystack = " ".join(row.values()).lower()
        hits = sum(1 for token in tokens if token in haystack)
        if hits:
            scored.append((hits == len(tokens), hits, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored[:limit]]


def format_rows(rows: list[dict[str, str]]) -> str:
    """One line per row: ``- header: value | header: value`` (empty cells skipped)."""
    return "\n".join("- " + " | ".join(f"{key}: {value}" for key, value in row.items() if value) for row in rows)
