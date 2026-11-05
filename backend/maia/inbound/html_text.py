"""HTML → plain text for inbound Gmail bodies (used by ``maia/inbound/save/email.py``)."""

import logging
import re
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


class HTMLTextExtractor(HTMLParser):
    """HTMLParser that collects the text nodes of an HTML body."""
    def __init__(self):
        """Start with an empty list of text parts."""
        super().__init__()
        self.text_parts = []

    def handle_data(self, data):
        """Collect a text node."""
        self.text_parts.append(data)

    def join_text_parts(self):
        """The collected text, joined."""
        return "".join(self.text_parts)


def _feed_and_collect_text(text_extractor, raw_html):
    """Feed the HTML to the extractor and return its text nodes joined, runs of whitespace collapsed to one space and
    the ends stripped. Parser errors propagate: the caller falls back to the regex.
    """
    text_extractor.feed(raw_html)
    plain_text = text_extractor.join_text_parts()
    # Clean up extra whitespace and line breaks
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    return plain_text


def _strip_html_tags_with_regex(raw_html):
    """Fallback when the parser fails: remove every <...> tag with a regex and strip the ends (inner whitespace is
    kept as it is).
    """
    html_tag_pattern = re.compile("<.*?>")
    return re.sub(html_tag_pattern, "", raw_html).strip()


def extract_text_from_html(raw_html):
    """Convert HTML content to plain text"""
    if not raw_html:
        return ""

    # Create text extractor instance
    text_extractor = HTMLTextExtractor()
    try:
        return _feed_and_collect_text(text_extractor, raw_html)
    except Exception as error:
        logger.error(f"Error parsing HTML: {error}")
        # Fallback: use regex to strip HTML tags
        return _strip_html_tags_with_regex(raw_html)
