"""Extracts text from XLSX files for ingestion."""

import requests
from io import BytesIO
from openpyxl import load_workbook
import xlrd
import pandas as pd


def read_xlsx(filename: str) -> str:
    """
    Reads Excel files (.xls or .xlsx) and converts them to a structured CSV-like format.
    Uses pandas for better structure and easier AI processing.
    """
    file_text = ""

    try:
        if filename.startswith("http://") or filename.startswith("https://"):
            # Download the file first
            response = requests.get(filename)
            response.raise_for_status()
            file_stream = BytesIO(response.content)

            # Let pandas handle both .xls and .xlsx automatically
            excel_file = pd.ExcelFile(file_stream)
        else:
            # Local file path
            excel_file = pd.ExcelFile(filename)

        # Process each sheet
        for sheet_name in excel_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet_name)

            # Skip empty sheets
            if df.empty:
                continue

            # Add sheet header with metadata
            file_text += f"=== SHEET: {sheet_name} ===\n"
            file_text += f"Rows: {len(df)}, Columns: {len(df.columns)}\n\n"

            # Convert to CSV format (comma-separated, proper escaping)
            csv_content = df.to_csv(index=False, lineterminator="\n")
            file_text += csv_content

            # Add separator between sheets
            file_text += "\n" + "=" * 50 + "\n\n"

        return file_text.strip()

    except Exception:
        # Fallback to original method if pandas fails
        return _read_xlsx_fallback(filename)


def _read_xlsx_fallback(filename: str) -> str:
    """
    Fallback method using openpyxl/xlrd directly.
    """
    file_text = ""
    is_xls = filename.lower().endswith(".xls") and not filename.lower().endswith(
        ".xlsx"
    )

    if filename.startswith("http://") or filename.startswith("https://"):
        response = requests.get(filename)
        response.raise_for_status()
        file_stream = BytesIO(response.content)

        if is_xls:
            workbook = xlrd.open_workbook(file_contents=file_stream.getvalue())
            for sheet_idx in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(sheet_idx)
                file_text += f"=== SHEET: {sheet.name} ===\n"
                for row_idx in range(sheet.nrows):
                    row = sheet.row_values(row_idx)
                    file_text += (
                        ",".join(
                            f'"{str(cell)}"' if "," in str(cell) else str(cell)
                            for cell in row
                        )
                        + "\n"
                    )
                file_text += "\n"
        else:
            workbook = load_workbook(file_stream)
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                file_text += f"=== SHEET: {sheet_name} ===\n"
                for row in sheet.iter_rows(values_only=True):
                    csv_row = ",".join(
                        (
                            f'"{str(cell)}"'
                            if cell and "," in str(cell)
                            else str(cell or "")
                        )
                        for cell in row
                    )
                    file_text += csv_row + "\n"
                file_text += "\n"
    else:
        if is_xls:
            workbook = xlrd.open_workbook(filename)
            for sheet_idx in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(sheet_idx)
                file_text += f"=== SHEET: {sheet.name} ===\n"
                for row_idx in range(sheet.nrows):
                    row = sheet.row_values(row_idx)
                    file_text += (
                        ",".join(
                            f'"{str(cell)}"' if "," in str(cell) else str(cell)
                            for cell in row
                        )
                        + "\n"
                    )
                file_text += "\n"
        else:
            workbook = load_workbook(filename)
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                file_text += f"=== SHEET: {sheet_name} ===\n"
                for row in sheet.iter_rows(values_only=True):
                    csv_row = ",".join(
                        (
                            f'"{str(cell)}"'
                            if cell and "," in str(cell)
                            else str(cell or "")
                        )
                        for cell in row
                    )
                    file_text += csv_row + "\n"
                file_text += "\n"

    return file_text.strip()
