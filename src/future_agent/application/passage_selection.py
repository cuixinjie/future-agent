"""Deterministic, bounded excerpt selection; lexical matches are not evidence validation."""

import re


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(run[i : i + 2] for i in range(len(run) - 1))
    return {word for word in words if len(word) >= 2}


def select_excerpt(text: str, target: str, limit: int) -> tuple[int, str]:
    """Select one continuous excerpt, falling back to the start when no terms match."""
    if limit <= 0 or not text:
        return 0, ""
    if len(text) <= limit:
        return 0, text
    terms = _terms(target)
    if not terms:
        return 0, text[:limit]
    # Overlapping windows reduce boundary loss; tie-breaking favors earlier text.
    step = max(1, limit // 2)
    starts = list(range(0, len(text) - limit + 1, step))
    starts.append(len(text) - limit)
    start = max(starts, key=lambda offset: len(terms & _terms(text[offset : offset + limit])))
    return start, text[start : start + limit]
