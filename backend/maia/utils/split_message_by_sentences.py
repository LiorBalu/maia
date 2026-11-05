"""Splits a long reply into platform-sized messages at sentence boundaries."""

import re


def _find_natural_cut_index(segment_text, max_segment_length):
    """The end index of the next piece of an oversized text: just after the last break marker found between
    max_segment_length // 4 and max_segment_length (or the end of the text), trying newline first, then period,
    Arabic comma, semicolon, comma and space; max_segment_length when no marker is found.
    """
    limit_midpoint = max_segment_length // 2
    window_start = limit_midpoint // 2
    window_end = min(len(segment_text), max_segment_length)
    cut_index = -1
    # Prefer newline, then period, then comma/semicolon, then space
    for break_marker in ["\n", ".", "،", ";", ",", " "]:
        # Search for the closest break to the middle, within the allowed range
        break_index = segment_text.rfind(break_marker, window_start, window_end)
        if break_index != -1:
            cut_index = break_index + len(break_marker)
            break
    if cut_index <= 0:
        # No break found — hard cut at max_segment_length
        cut_index = max_segment_length
    return cut_index


def _split_oversized_segment(segment_text, max_segment_length):
    """Split a chunk that exceeds max_segment_length at a natural break point near the middle."""
    segments = []
    remaining_text = segment_text
    while len(remaining_text) > max_segment_length:
        cut_index = _find_natural_cut_index(remaining_text, max_segment_length)
        segments.append(remaining_text[:cut_index].strip())
        remaining_text = remaining_text[cut_index:].strip()
    if remaining_text:
        segments.append(remaining_text)
    return segments


def _pack_sentences_into_segments(sentences, max_segment_length):
    """Consecutive whole sentences packed greedily into stripped segments of at most max_segment_length characters
    (a sentence longer than the limit becomes a segment of its own); empty segments are dropped.
    """
    sentence_segments = []
    pending_segment = ""

    for sentence in sentences:
        if len(pending_segment) + len(sentence) + 1 <= max_segment_length:  # +1 for the space
            pending_segment += sentence + " "
        else:
            if pending_segment.strip():
                sentence_segments.append(pending_segment.strip())
            pending_segment = sentence + " "

    if pending_segment.strip():
        sentence_segments.append(pending_segment.strip())

    return sentence_segments


def _enforce_segment_length_limit(segments, max_segment_length):
    """The segments in order, each one longer than max_segment_length replaced by its natural-break pieces."""
    bounded_segments = []
    for segment in segments:
        if len(segment) > max_segment_length:
            bounded_segments.extend(_split_oversized_segment(segment, max_segment_length))
        else:
            bounded_segments.append(segment)

    return bounded_segments


def split_message_into_segments(reply_text, max_segment_length):
    """Split a text into parts of at most max_segment_length characters at sentence boundaries (. ! ?), cutting
    oversized sentences at natural breaks.
    """
    sentences = re.split(r"(?<=[.!?])\s", reply_text)
    sentence_segments = _pack_sentences_into_segments(sentences, max_segment_length)

    # Handle any parts that still exceed max_segment_length
    return _enforce_segment_length_limit(sentence_segments, max_segment_length)
