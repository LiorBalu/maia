"""Extract the text of a PDF with pypdf, falling back to Tesseract OCR for scanned documents."""

import logging
from io import BytesIO

import pytesseract
import requests
from pdf2image import convert_from_bytes
from pypdf import PdfReader
from pypdf.errors import PdfStreamError

logger = logging.getLogger(__name__)


def _load_pdf_bytes(file_path_or_url: str) -> bytes:
    """Bytes of a PDF from a URL or a local path."""
    if file_path_or_url.startswith(("http://", "https://")):
        response = requests.get(file_path_or_url, timeout=120)
        response.raise_for_status()
        return response.content
    with open(file_path_or_url, "rb") as pdf_file:
        return pdf_file.read()


def _ocr_pdf(pdf_binary: bytes) -> str:
    """OCR every page, trying the four orientations and keeping the longest result."""
    final_text = ""
    for image in convert_from_bytes(pdf_binary, dpi=300):
        best_text, max_len = "", 0
        for angle in (0, 90, 180, 270):
            text = pytesseract.image_to_string(image.rotate(angle, expand=True), lang="heb+eng")
            if len(text.strip()) > max_len:
                best_text, max_len = text, len(text.strip())
        final_text += best_text + "\n"
    return final_text


def read_pdf(file_path_or_url: str) -> str:
    """Return the text of a PDF (URL or local path). Uses OCR when the PDF has no selectable text."""
    pdf_binary = _load_pdf_bytes(file_path_or_url)
    try:
        reader = PdfReader(BytesIO(pdf_binary))
        extracted_text = "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
    except PdfStreamError:
        logger.error("PDF stream error – falling back to OCR.")
        extracted_text = ""

    if not extracted_text.strip() or "camscanner" in extracted_text.lower() or "rfr_7" in extracted_text.lower():
        logger.warning("No text found – trying OCR with orientation detection.")
        return _ocr_pdf(pdf_binary)
    return extracted_text
