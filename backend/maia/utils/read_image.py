"""Reads an image attachment (OCR / description) into text for the bot."""

from io import BytesIO
import pytesseract
import requests
from PIL import Image


def extract_text_from_image(image_url):
    """Hebrew OCR (Tesseract) of an image URL."""
    image_response = requests.get(image_url)
    decoded_image = Image.open(BytesIO(image_response.content))
    recognized_text = pytesseract.image_to_string(decoded_image, lang="heb") + "\n"
    return recognized_text
