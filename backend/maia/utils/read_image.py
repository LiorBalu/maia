"""Reads an image attachment (OCR / description) into text for the bot."""

from io import BytesIO
import pytesseract
import requests
from PIL import Image


def read_image(img_url):
    """Hebrew OCR (Tesseract) of an image URL."""
    response = requests.get(img_url)
    img = Image.open(BytesIO(response.content))
    extracted_text = pytesseract.image_to_string(img, lang="heb") + "\n"
    return extracted_text
