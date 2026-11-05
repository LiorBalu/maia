"""Extract the text of a PDF with pypdf, falling back to Tesseract OCR for scanned documents."""

import logging
from io import BytesIO

import pytesseract
import requests
from pdf2image import convert_from_bytes
from pypdf import PdfReader
from pypdf.errors import PdfStreamError

logger = logging.getLogger(__name__)


def _fetch_pdf_bytes(file_path_or_url: str) -> bytes:
    """Bytes of a PDF from a URL or a local path."""
    if file_path_or_url.startswith(("http://", "https://")):
        download_response = requests.get(file_path_or_url, timeout=120)
        download_response.raise_for_status()
        return download_response.content
    with open(file_path_or_url, "rb") as pdf_file_handle:
        return pdf_file_handle.read()


def _recognize_page_text_in_best_rotation(page_image) -> str:
    """OCR one page image in the four orientations and return the longest text Tesseract recognized ("" if none)."""
    longest_text, longest_text_length = "", 0
    for rotation_degrees in (0, 90, 180, 270):
        recognized_text = pytesseract.image_to_string(page_image.rotate(rotation_degrees, expand=True), lang="heb+eng")
        if len(recognized_text.strip()) > longest_text_length:
            longest_text, longest_text_length = recognized_text, len(recognized_text.strip())
    return longest_text


def _extract_text_from_scanned_pdf(pdf_bytes: bytes) -> str:
    """OCR every page, trying the four orientations and keeping the longest result."""
    all_pages_text = ""
    for page_image in convert_from_bytes(pdf_bytes, dpi=300):
        all_pages_text += _recognize_page_text_in_best_rotation(page_image) + "\n"
    return all_pages_text


def _extract_selectable_text(pdf_bytes: bytes) -> str:
    """The PDF's text layer (its non-empty pages joined by newlines), or "" after logging a pypdf PdfStreamError."""
    try:
        pdf_reader = PdfReader(BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() for page in pdf_reader.pages if page.extract_text())
    except PdfStreamError:
        logger.error("PDF stream error – falling back to OCR.")
        return ""


def _is_text_layer_unusable(text_layer: str) -> bool:
    """True when the text layer must be replaced by OCR: blank, a CamScanner watermark or rfr_7 glyph garbage."""
    return not text_layer.strip() or "camscanner" in text_layer.lower() or "rfr_7" in text_layer.lower()


def extract_text_from_pdf(file_path_or_url: str) -> str:
    """Return the text of a PDF (URL or local path). Uses OCR when the PDF has no selectable text."""
    pdf_bytes = _fetch_pdf_bytes(file_path_or_url)
    selectable_text = _extract_selectable_text(pdf_bytes)

    if _is_text_layer_unusable(selectable_text):
        logger.warning("No text found – trying OCR with orientation detection.")
        return _extract_text_from_scanned_pdf(pdf_bytes)
    return selectable_text
