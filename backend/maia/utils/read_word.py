"""Extracts text (and tables as CSV) from DOCX files for ingestion."""

import logging
import requests
from io import BytesIO
from docx import Document
import zipfile
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# Define namespaces
WORD_XML_NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}


def _escape_csv_cell(cell_value) -> str:
    """The CSV-safe text of one table cell: stripped, and wrapped in quotes (inner quotes doubled) when it holds a
    comma, a double quote or a newline.
    """
    cell_text = str(cell_value).strip()
    if "," in cell_text or '"' in cell_text or "\n" in cell_text:
        # Escape quotes and wrap in quotes
        cell_text = '"' + cell_text.replace('"', '""') + '"'
    return cell_text


def _render_table_rows_as_csv(table_rows: list) -> str:
    """
    Convert table data to CSV format for better AI processing.

    Args:
        table_rows: List of lists representing table rows

    Returns:
        CSV formatted string
    """
    if not table_rows:
        return ""

    csv_text = ""
    for row in table_rows:
        # Escape cells that contain commas or quotes
        escaped_cells = [_escape_csv_cell(cell) for cell in row]
        csv_text += ",".join(escaped_cells) + "\n"

    return csv_text


def _render_table_section(table_number: int, table_rows: list[list[str]], closing_rule: str) -> str:
    """The text block of one non-empty table: the '=== TABLE n ===' header, its row and column counts, its rows as
    CSV, then closing_rule.
    """
    table_section = f"\n=== TABLE {table_number} ===\n"
    table_section += f"Rows: {len(table_rows)}, "
    table_section += f"Columns: {len(table_rows[0])}\n\n"
    table_section += _render_table_rows_as_csv(table_rows)
    table_section += closing_rule
    return table_section


def _load_word_file_stream(file_path_or_url: str) -> BytesIO:
    """The bytes of a DOCX file in a BytesIO: downloaded when an http(s) URL, read from disk otherwise."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        download_response = requests.get(file_path_or_url)
        download_response.raise_for_status()
        word_stream = BytesIO(download_response.content)
    else:
        with open(file_path_or_url, "rb") as word_file:
            word_stream = BytesIO(word_file.read())
    return word_stream


def _collect_xml_text_runs(element) -> str:
    """The concatenated text of every w:t text element under a w:p or w:tc element (unstripped)."""
    run_text = ""
    for text_element in element.findall(".//w:t", WORD_XML_NAMESPACES):
        if text_element.text:
            run_text += text_element.text
    return run_text


def _collect_table_rows_from_xml(table_element) -> list[list[str]]:
    """The stripped cell texts of each w:tr row of a w:tbl element, keeping only the rows with a non-empty cell."""
    table_rows = []
    for row_element in table_element.findall(".//w:tr", WORD_XML_NAMESPACES):
        row_cells = []
        for cell_element in row_element.findall(".//w:tc", WORD_XML_NAMESPACES):
            row_cells.append(_collect_xml_text_runs(cell_element).strip())
        if any(row_cells):  # Only add non-empty rows
            table_rows.append(row_cells)
    return table_rows


def _extract_text_from_document_xml(document_xml: bytes) -> str:
    """The text of word/document.xml: its non-blank paragraphs (table cells included), then each non-empty table as
    a CSV section.
    """
    document_root = ET.fromstring(document_xml)
    file_text = ""

    # Extract all text from paragraphs
    for paragraph_element in document_root.findall(".//w:p", WORD_XML_NAMESPACES):
        paragraph_text = _collect_xml_text_runs(paragraph_element)
        if paragraph_text.strip():
            file_text += paragraph_text + "\n"

    # Extract text from tables
    table_number = 0
    for table_element in document_root.findall(".//w:tbl", WORD_XML_NAMESPACES):
        table_number += 1
        table_rows = _collect_table_rows_from_xml(table_element)

        if table_rows:
            file_text += _render_table_section(table_number, table_rows, "=" * 50 + "\n\n")

    return file_text


def _extract_text_from_word_zip_archive(file_path_or_url: str) -> str:
    """
    Fallback method to read Word documents by parsing the XML directly.
    Used when python-docx fails due to corrupted relationships or NULL references.
    """
    word_stream = _load_word_file_stream(file_path_or_url)

    file_text = ""

    try:
        # Open as ZIP and extract word/document.xml
        with zipfile.ZipFile(word_stream, "r") as word_archive:
            # Read the main document XML
            if "word/document.xml" in word_archive.namelist():
                document_xml = word_archive.read("word/document.xml")
                file_text = _extract_text_from_document_xml(document_xml)

    except Exception as error:
        raise Exception(f"Failed to read Word document with fallback method: {error}")

    return file_text


def _open_word_document(file_path_or_url: str):
    """Open a DOCX with python-docx: downloaded into memory when an http(s) URL, opened from its path otherwise."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        download_response = requests.get(file_path_or_url)
        download_response.raise_for_status()
        word_stream = BytesIO(download_response.content)
        document = Document(word_stream)
    else:
        document = Document(file_path_or_url)
    return document


def _collect_paragraph_text(paragraph_element) -> str:
    """The concatenated text of the children of a body paragraph element that carry text (unstripped)."""
    paragraph_text = ""
    for child_node in paragraph_element.iterchildren():
        if hasattr(child_node, "text") and child_node.text:
            paragraph_text += child_node.text
    return paragraph_text


def _find_table_for_element(document, body_element):
    """The (1-based table number, python-docx Table) of the first table of the document whose XML element is
    body_element, or None when no table matches.
    """
    for table_number, table in enumerate(document.tables, start=1):
        if table._element == body_element:
            return table_number, table
    return None


def _collect_table_rows(table) -> list[list[str]]:
    """The stripped cell texts of each row of a python-docx Table, keeping only the rows with a non-empty cell."""
    table_rows = []
    for row in table.rows:
        row_cells = [cell.text.strip() for cell in row.cells]
        if any(row_cells):  # Only add non-empty rows
            table_rows.append(row_cells)
    return table_rows


def _collect_document_body_text(document) -> str:
    """The text of the document body in order: each non-blank paragraph followed by six newlines, each non-empty
    table as a CSV section.
    """
    file_text = ""
    for body_element in document.element.body:
        if body_element.tag.endswith("p"):
            paragraph_text = _collect_paragraph_text(body_element)
            if paragraph_text.strip():
                file_text += paragraph_text + "\n\n\n\n\n\n"

        elif body_element.tag.endswith("tbl"):
            matching_table = _find_table_for_element(document, body_element)
            if matching_table is not None:
                table_number, table = matching_table
                table_rows = _collect_table_rows(table)

                if table_rows:
                    file_text += _render_table_section(table_number, table_rows, "=" * 20 + "\n\n\n\n\n")

    return file_text


def extract_text_from_word(file_path_or_url: str) -> str:
    """
    Read Word document content. Tries python-docx first, falls back to XML parsing if needed.

    Args:
        file_path_or_url: Path or URL to the Word document

    Returns:
        Extracted text content from the document
    """
    try:
        # Try the normal python-docx approach first
        document = _open_word_document(file_path_or_url)
        return _collect_document_body_text(document)

    except KeyError as error:
        # Handle "There is no item named 'word/NULL' in the archive" error
        if "word/NULL" in str(error) or "NULL" in str(error):
            logger.error(" Document has corrupted references, using fallback XML parser")
            return _extract_text_from_word_zip_archive(file_path_or_url)
        else:
            raise

    except Exception as error:
        # For other errors, try the fallback method
        error_type = type(error).__name__
        logger.error(f" python-docx failed ({error_type}), trying fallback XML parser")
        try:
            return _extract_text_from_word_zip_archive(file_path_or_url)
        except Exception as fallback_error:
            error_message = (
                f"Failed to read Word document with both methods. "
                f"Original error: {error}, Fallback error: {fallback_error}"
            )
            raise Exception(error_message)
