"""Reads a text attachment for the bot."""

import requests

def read_txt(filename: str) -> str:
    if filename.startswith("http://") or filename.startswith("https://"):
        response = requests.get(filename)
        response.raise_for_status()
        # Assume UTF-8 encoding here
        file_text = response.content.decode("utf-8")
    else:
        with open(filename, "r", encoding="utf-8") as f:
            file_text = f.read()
    return file_text