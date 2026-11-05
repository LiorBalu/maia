"""Reads a text attachment for the bot."""

import requests

def extract_text_from_txt(file_path_or_url: str) -> str:
    """Text of a UTF-8 file (URL or path)."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        http_response = requests.get(file_path_or_url)
        http_response.raise_for_status()
        # Assume UTF-8 encoding here
        attachment_text = http_response.content.decode("utf-8")
    else:
        with open(file_path_or_url, "r", encoding="utf-8") as text_file:
            attachment_text = text_file.read()
    return attachment_text