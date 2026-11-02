"""Extracts text (and tables as CSV) from DOCX files for ingestion."""

import logging
import requests
from io import BytesIO
from docx import Document
import zipfile
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


def _format_table_as_csv(table_data: list) -> str:
    """
    Convert table data to CSV format for better AI processing.

    Args:
        table_data: List of lists representing table rows

    Returns:
        CSV formatted string
    """
    if not table_data:
        return ""

    csv_text = ""
    for row in table_data:
        # Escape cells that contain commas or quotes
        escaped_row = []
        for cell in row:
            cell_str = str(cell).strip()
            if "," in cell_str or '"' in cell_str or "\n" in cell_str:
                # Escape quotes and wrap in quotes
                cell_str = '"' + cell_str.replace('"', '""') + '"'
            escaped_row.append(cell_str)
        csv_text += ",".join(escaped_row) + "\n"

    return csv_text


def read_word_with_fallback(filename: str) -> str:
    """
    Fallback method to read Word documents by parsing the XML directly.
    Used when python-docx fails due to corrupted relationships or NULL references.
    """
    if filename.startswith("http://") or filename.startswith("https://"):
        response = requests.get(filename)
        response.raise_for_status()
        file_stream = BytesIO(response.content)
    else:
        with open(filename, "rb") as f:
            file_stream = BytesIO(f.read())

    file_text = ""

    try:
        # Open as ZIP and extract word/document.xml
        with zipfile.ZipFile(file_stream, "r") as docx_zip:
            # Read the main document XML
            if "word/document.xml" in docx_zip.namelist():
                xml_content = docx_zip.read("word/document.xml")
                root = ET.fromstring(xml_content)

                # Define namespaces
                namespaces = {
                    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
                }

                # Extract all text from paragraphs
                for paragraph in root.findall(".//w:p", namespaces):
                    paragraph_text = ""
                    for text_elem in paragraph.findall(".//w:t", namespaces):
                        if text_elem.text:
                            paragraph_text += text_elem.text
                    if paragraph_text.strip():
                        file_text += paragraph_text + "\n"

                # Extract text from tables
                table_count = 0
                for table in root.findall(".//w:tbl", namespaces):
                    table_count += 1
                    table_data = []

                    for row in table.findall(".//w:tr", namespaces):
                        row_data = []
                        for cell in row.findall(".//w:tc", namespaces):
                            cell_text = ""
                            for text_elem in cell.findall(".//w:t", namespaces):
                                if text_elem.text:
                                    cell_text += text_elem.text
                            row_data.append(cell_text.strip())
                        if any(row_data):  # Only add non-empty rows
                            table_data.append(row_data)

                    if table_data:
                        file_text += f"\n=== TABLE {table_count} ===\n"
                        file_text += f"Rows: {len(table_data)}, "
                        file_text += f"Columns: {len(table_data[0])}\n\n"
                        file_text += _format_table_as_csv(table_data)
                        file_text += "=" * 50 + "\n\n"

    except Exception as e:
        raise Exception(f"Failed to read Word document with fallback method: {e}")

    return file_text


def read_word(filename: str) -> str:
    """
    Read Word document content. Tries python-docx first, falls back to XML parsing if needed.

    Args:
        filename: Path or URL to the Word document

    Returns:
        Extracted text content from the document
    """
    file_text = ""

    try:
        # Try the normal python-docx approach first
        if filename.startswith("http://") or filename.startswith("https://"):
            response = requests.get(filename)
            response.raise_for_status()
            file_stream = BytesIO(response.content)
            document = Document(file_stream)
        else:
            document = Document(filename)

        for element in document.element.body:
            if element.tag.endswith("p"):
                paragraph_text = ""
                for node in element.iterchildren():
                    if hasattr(node, "text") and node.text:
                        paragraph_text += node.text
                if paragraph_text.strip():
                    file_text += paragraph_text + "\n\n\n\n\n\n"

            elif element.tag.endswith("tbl"):
                for table_idx, table in enumerate(document.tables, start=1):
                    if table._element == element:
                        table_data = []

                        for row in table.rows:
                            row_data = [cell.text.strip() for cell in row.cells]
                            if any(row_data):  # Only add non-empty rows
                                table_data.append(row_data)

                        if table_data:
                            file_text += f"\n=== TABLE {table_idx} ===\n"
                            file_text += f"Rows: {len(table_data)}, "
                            file_text += f"Columns: {len(table_data[0])}\n\n"
                            file_text += _format_table_as_csv(table_data)
                            file_text += "=" * 20 + "\n\n\n\n\n"
                        break

        return file_text

    except KeyError as e:
        # Handle "There is no item named 'word/NULL' in the archive" error
        if "word/NULL" in str(e) or "NULL" in str(e):
            logger.error(" Document has corrupted references, using fallback XML parser")
            return read_word_with_fallback(filename)
        else:
            raise

    except Exception as e:
        # For other errors, try the fallback method
        error_type = type(e).__name__
        logger.error(f" python-docx failed ({error_type}), trying fallback XML parser")
        try:
            return read_word_with_fallback(filename)
        except Exception as fallback_error:
            error_msg = (
                f"Failed to read Word document with both methods. "
                f"Original error: {e}, Fallback error: {fallback_error}"
            )
            raise Exception(error_msg)
