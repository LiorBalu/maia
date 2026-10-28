"""Reads a CSV attachment into text for the bot."""

import requests
from io import StringIO
import csv


def read_csv(filename: str) -> str:
    """A CSV file (URL or path) as tab-separated lines of text."""
    file_text = ""
    if filename.startswith("http://") or filename.startswith("https://"):
        # Download the remote CSV file
        response = requests.get(filename)
        response.raise_for_status()
        # Assume UTF-8 encoding here
        file_stream = StringIO(response.content.decode("utf-8"))
    else:
        file_stream = open(filename, "r", encoding="utf-8")

    with file_stream:
        reader = csv.reader(file_stream)
        for row in reader:
            file_text += "\t".join(row) + "\n"
    return file_text
