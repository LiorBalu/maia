"""HTML → plain text for inbound Gmail bodies (used by ``maia/inbound/save/email.py``)."""

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []

    def handle_data(self, data):
        self.text_parts.append(data)

    def get_text(self):
        return "".join(self.text_parts)


def html_to_text(html_content):
    """Convert HTML content to plain text"""
    if not html_content:
        return ""

    # Create parser instance
    parser = HTMLTextExtractor()
    try:
        parser.feed(html_content)
        text = parser.get_text()
        # Clean up extra whitespace and line breaks
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        logger.error(f"Error parsing HTML: {e}")
        # Fallback: use regex to strip HTML tags
        clean = re.compile("<.*?>")
        return re.sub(clean, "", html_content).strip()
