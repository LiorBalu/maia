"""Extracts text from XLSX files for ingestion."""

import requests
from io import BytesIO
from openpyxl import Workbook, load_workbook
import xlrd
import pandas as pd


def _download_to_stream(url: str) -> BytesIO:
    """The body of a GET request to url in a BytesIO (raises requests.HTTPError on an error status)."""
    download_response = requests.get(url)
    download_response.raise_for_status()
    return BytesIO(download_response.content)


def _open_excel_workbook(file_path_or_url: str) -> pd.ExcelFile:
    """Open an .xls or .xlsx file (downloaded first when an http(s) URL) as a pandas ExcelFile."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        # Download the file first
        workbook_stream = _download_to_stream(file_path_or_url)

        # Let pandas handle both .xls and .xlsx automatically
        excel_workbook = pd.ExcelFile(workbook_stream)
    else:
        # Local file path
        excel_workbook = pd.ExcelFile(file_path_or_url)
    return excel_workbook


def _extract_sheets_with_pandas(excel_workbook: pd.ExcelFile) -> str:
    """Each non-empty sheet as a '=== SHEET ===' header, its row and column counts, its rows as CSV and a separator
    line (unstripped).
    """
    sheets_text = ""

    # Process each sheet
    for sheet_name in excel_workbook.sheet_names:
        sheet_frame = pd.read_excel(excel_workbook, sheet_name=sheet_name)

        # Skip empty sheets
        if sheet_frame.empty:
            continue

        # Add sheet header with metadata
        sheets_text += f"=== SHEET: {sheet_name} ===\n"
        sheets_text += f"Rows: {len(sheet_frame)}, Columns: {len(sheet_frame.columns)}\n\n"

        # Convert to CSV format (comma-separated, proper escaping)
        sheet_csv = sheet_frame.to_csv(index=False, lineterminator="\n")
        sheets_text += sheet_csv

        # Add separator between sheets
        sheets_text += "\n" + "=" * 50 + "\n\n"

    return sheets_text


def extract_text_from_xlsx(file_path_or_url: str) -> str:
    """
    Reads Excel files (.xls or .xlsx) and converts them to a structured CSV-like format.
    Uses pandas for better structure and easier AI processing.
    """
    try:
        excel_workbook = _open_excel_workbook(file_path_or_url)
        sheets_text = _extract_sheets_with_pandas(excel_workbook)
        return sheets_text.strip()

    except Exception:
        # Fallback to original method if pandas fails
        return _extract_text_from_xlsx_without_pandas(file_path_or_url)


def _render_xlrd_workbook(xlrd_workbook: xlrd.Book) -> str:
    """Each sheet of an xlrd Book (.xls) as a '=== SHEET ===' header and its rows joined by commas (a cell holding a
    comma is quoted), then a blank line (unstripped).
    """
    sheets_text = ""
    for sheet_index in range(xlrd_workbook.nsheets):
        sheet = xlrd_workbook.sheet_by_index(sheet_index)
        sheets_text += f"=== SHEET: {sheet.name} ===\n"
        for row_index in range(sheet.nrows):
            row_values = sheet.row_values(row_index)
            sheets_text += (
                ",".join(
                    f'"{str(cell)}"' if "," in str(cell) else str(cell)
                    for cell in row_values
                )
                + "\n"
            )
        sheets_text += "\n"
    return sheets_text


def _render_openpyxl_workbook(openpyxl_workbook: Workbook) -> str:
    """Each sheet of an openpyxl Workbook (.xlsx) as a '=== SHEET ===' header and its rows joined by commas (a cell
    holding a comma is quoted, a falsy cell such as None, 0 or False is ''), then a blank line (unstripped).
    """
    sheets_text = ""
    for sheet_name in openpyxl_workbook.sheetnames:
        sheet = openpyxl_workbook[sheet_name]
        sheets_text += f"=== SHEET: {sheet_name} ===\n"
        for row_values in sheet.iter_rows(values_only=True):
            csv_row = ",".join(
                (
                    f'"{str(cell)}"'
                    if cell and "," in str(cell)
                    else str(cell or "")
                )
                for cell in row_values
            )
            sheets_text += csv_row + "\n"
        sheets_text += "\n"
    return sheets_text


def _extract_text_from_xlsx_without_pandas(file_path_or_url: str) -> str:
    """
    Fallback method using openpyxl/xlrd directly.
    """
    is_legacy_xls = file_path_or_url.lower().endswith(".xls") and not file_path_or_url.lower().endswith(
        ".xlsx"
    )

    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        workbook_stream = _download_to_stream(file_path_or_url)

        if is_legacy_xls:
            xlrd_workbook = xlrd.open_workbook(file_contents=workbook_stream.getvalue())
            sheets_text = _render_xlrd_workbook(xlrd_workbook)
        else:
            openpyxl_workbook = load_workbook(workbook_stream)
            sheets_text = _render_openpyxl_workbook(openpyxl_workbook)
    else:
        if is_legacy_xls:
            xlrd_workbook = xlrd.open_workbook(file_path_or_url)
            sheets_text = _render_xlrd_workbook(xlrd_workbook)
        else:
            openpyxl_workbook = load_workbook(file_path_or_url)
            sheets_text = _render_openpyxl_workbook(openpyxl_workbook)

    return sheets_text.strip()
