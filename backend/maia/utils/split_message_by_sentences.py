"""Splits a long reply into platform-sized messages at sentence boundaries."""

import re


def _split_oversized_chunk(text, max_length):
    """Split a chunk that exceeds max_length at a natural break point near the middle."""
    results = []
    while len(text) > max_length:
        mid = max_length // 2
        search_start = mid // 2
        search_end = min(len(text), max_length)
        best = -1
        # Prefer newline, then period, then comma/semicolon, then space
        for pattern in ["\n", ".", "،", ";", ",", " "]:
            # Search for the closest break to the middle, within the allowed range
            idx = text.rfind(pattern, search_start, search_end)
            if idx != -1:
                best = idx + len(pattern)
                break
        if best <= 0:
            # No break found — hard cut at max_length
            best = max_length
        results.append(text[:best].strip())
        text = text[best:].strip()
    if text:
        results.append(text)
    return results


def split_message_by_sentences(message, max_length):
    """Split a text into parts of at most max_length characters at sentence boundaries (. ! ?), cutting oversized
    sentences at natural breaks.
    """
    sentences = re.split(r"(?<=[.!?])\s", message)
    parts = []
    current_part = ""

    for sentence in sentences:
        if len(current_part) + len(sentence) + 1 <= max_length:  # +1 for the space
            current_part += sentence + " "
        else:
            if current_part.strip():
                parts.append(current_part.strip())
            current_part = sentence + " "

    if current_part.strip():
        parts.append(current_part.strip())

    # Handle any parts that still exceed max_length
    final_parts = []
    for part in parts:
        if len(part) > max_length:
            final_parts.extend(_split_oversized_chunk(part, max_length))
        else:
            final_parts.append(part)

    return final_parts
