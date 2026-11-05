"""Reads a CSV attachment into text for the bot."""

import requests
from io import StringIO, TextIOBase
import csv


def _open_csv_source(file_path_or_url: str) -> TextIOBase:
    """A text stream of the CSV: the downloaded body decoded as UTF-8 for an http(s) URL, the opened file otherwise."""
    if file_path_or_url.startswith("http://") or file_path_or_url.startswith("https://"):
        # Download the remote CSV file
        download_response = requests.get(file_path_or_url)
        download_response.raise_for_status()
        # Assume UTF-8 encoding here
        csv_stream = StringIO(download_response.content.decode("utf-8"))
    else:
        csv_stream = open(file_path_or_url, "r", encoding="utf-8")
    return csv_stream


def extract_text_from_csv(file_path_or_url: str) -> str:
    """A CSV file (URL or path) as tab-separated lines of text."""
    tab_separated_text = ""
    with _open_csv_source(file_path_or_url) as csv_stream:
        csv_row_reader = csv.reader(csv_stream)
        for cells in csv_row_reader:
            tab_separated_text += "\t".join(cells) + "\n"
    return tab_separated_text
