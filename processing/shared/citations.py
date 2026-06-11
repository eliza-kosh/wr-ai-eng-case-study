"""Citation validation helpers for generated summaries."""

from __future__ import annotations

import re

_ITEM_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:-])"
    r"(?:reddit|hacker_news|github|hn_story|hn_comment|reddit_post|reddit_comment):[A-Za-z0-9_-]+"
)


def extract_cited_item_ids(text: str) -> set[str]:
    """Extract likely source item IDs from generated text."""
    return set(_ITEM_ID_PATTERN.findall(text or ""))


def find_invalid_citations(text: str, allowed_item_ids: set[str]) -> set[str]:
    """Return cited item IDs that were not supplied in context or search results."""
    return extract_cited_item_ids(text) - allowed_item_ids
