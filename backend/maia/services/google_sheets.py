"""Read-only access to a tenant's Google Sheet through the Maia service account.

The business shares its spreadsheet (for example an inventory list: product, SKU, price, stock)
with the service account in ``serviceKey.json``; the bot then looks rows up in real time with the
``st_find_sheet_rows`` tool, and the dashboard shows a preview on the connection screen.

Only the first tab is read. The first row is the header; every other non-empty row becomes a
``{header: cell}`` dict. Reads are cached for a minute so a conversation with several questions
does not hit the Sheets API on every turn.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SERVICE_ACCOUNT_KEY_PATH = "serviceKey.json"
SHEETS_READONLY_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEET_CACHE_MAX_AGE_SECONDS = 60

_SHEET_ID_IN_URL_PATTERN = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_SHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class SheetAccessError(Exception):
    """The spreadsheet does not exist or is not shared with the service account."""


@dataclass
class SheetData:
    """Title, headers and rows of the first tab of a sheet."""
    spreadsheet_title: str
    column_headers: list[str]
    data_rows: list[dict[str, str]]


_cached_sheets_by_id: dict[str, tuple[float, SheetData]] = {}


def load_service_account_email() -> str:
    """The address the business has to share its sheet with."""
    with open(SERVICE_ACCOUNT_KEY_PATH, encoding="utf-8") as key_file:
        return json.load(key_file).get("client_email", "")


def resolve_sheet_id(sheet_url_or_id: str) -> str | None:
    """Spreadsheet id from a docs.google.com link, or the bare id itself; None when neither."""
    candidate_text = (sheet_url_or_id or "").strip()
    link_match = _SHEET_ID_IN_URL_PATTERN.search(candidate_text)
    if link_match:
        return link_match.group(1)
    return candidate_text if _BARE_SHEET_ID_PATTERN.match(candidate_text) else None


def _build_sheets_client():
    """Sheets API client authenticated with the service account."""
    service_account_credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_KEY_PATH, scopes=SHEETS_READONLY_SCOPES
    )
    return build("sheets", "v4", credentials=service_account_credentials, cache_discovery=False)


def _fetch_title_and_first_tab_cells(sheet_id: str) -> tuple[str, list[list[Any]]]:
    """The spreadsheet title and the raw cell rows of its first tab (header row included). Raises SheetAccessError
    when Google answers 403/404.
    """
    sheets_client = _build_sheets_client()
    try:
        spreadsheet_metadata = (
            sheets_client.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="properties.title,sheets.properties.title")
            .execute()
        )
        first_tab_title = spreadsheet_metadata["sheets"][0]["properties"]["title"]
        cell_rows = (
            sheets_client.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"'{first_tab_title}'").execute()
        ).get("values", [])
    except HttpError as exc:
        if exc.resp.status in (403, 404):
            raise SheetAccessError(
                f"The sheet is not reachable (HTTP {exc.resp.status}). "
                f"Share it with {load_service_account_email()} (Viewer is enough) and check the link."
            ) from exc
        raise
    return spreadsheet_metadata["properties"]["title"], cell_rows


def _split_headers_and_data_rows(cell_rows: list[list[Any]]) -> tuple[list[str], list[dict[str, str]]]:
    """The non-empty column headers of the first row, and one ``{header: cell}`` dict per non-empty row after it
    (short rows padded with empty cells).
    """
    header_cells = [str(cell).strip() for cell in (cell_rows[0] if cell_rows else [])]
    data_rows: list[dict[str, str]] = []
    for raw_row in cell_rows[1:]:
        cell_texts = [str(cell).strip() for cell in raw_row]
        if not any(cell_texts):
            continue
        cell_texts += [""] * (len(header_cells) - len(cell_texts))
        data_rows.append(
            {header: cell_texts[column_index] for column_index, header in enumerate(header_cells) if header}
        )
    return [header for header in header_cells if header], data_rows


def fetch_sheet_data(sheet_id: str, should_use_cache: bool = True) -> SheetData:
    """Title, headers and rows of the first tab. Raises SheetAccessError when Google refuses."""
    cached_entry = _cached_sheets_by_id.get(sheet_id)
    if should_use_cache and cached_entry and time.time() - cached_entry[0] < SHEET_CACHE_MAX_AGE_SECONDS:
        return cached_entry[1]

    spreadsheet_title, cell_rows = _fetch_title_and_first_tab_cells(sheet_id)
    column_headers, data_rows = _split_headers_and_data_rows(cell_rows)
    sheet_data = SheetData(spreadsheet_title=spreadsheet_title, column_headers=column_headers, data_rows=data_rows)
    _cached_sheets_by_id[sheet_id] = (time.time(), sheet_data)
    return sheet_data


def _split_query_words(query_text: str) -> list[str]:
    """The lowercase words of the query; single-character words are dropped when longer ones remain."""
    query_words = [word for word in re.split(r"\s+", (query_text or "").lower().strip()) if word]
    if len(query_words) > 1:
        # "PlayStation 5": a single character matches half of the sheet, so it only adds noise
        query_words = [word for word in query_words if len(word) > 1] or query_words
    return query_words


def find_matching_rows(sheet_rows: list[dict[str, str]], query_text: str, max_matches: int = 5) -> list[dict[str, str]]:
    """Rows whose cells contain the query words (case-insensitive); rows matching every word first."""
    query_words = _split_query_words(query_text)
    if not query_words:
        return []
    scored_rows = []
    for row in sheet_rows:
        row_text = " ".join(row.values()).lower()
        matched_word_count = sum(1 for word in query_words if word in row_text)
        if matched_word_count:
            scored_rows.append((matched_word_count == len(query_words), matched_word_count, row))
    scored_rows.sort(key=lambda scored_row: (scored_row[0], scored_row[1]), reverse=True)
    return [row for _, _, row in scored_rows[:max_matches]]


def render_rows_as_text(sheet_rows: list[dict[str, str]]) -> str:
    """One line per row: ``- header: value | header: value`` (empty cells skipped)."""
    return "\n".join(
        "- " + " | ".join(f"{header}: {cell}" for header, cell in row.items() if cell) for row in sheet_rows
    )
