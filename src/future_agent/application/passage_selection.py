"""Deterministic, bounded excerpt selection; lexical matches are not evidence validation."""

import re


def select_excerpts(text: str, target: str, limit: int) -> list[tuple[int, str]]:
    """Cover distinct target terms with up to three exact, non-overlapping passages."""
    if limit <= 0 or not text:
        return []
    if len(text) <= limit:
        return [(0, text)]
    terms = _terms(target)
    width = max(1, limit // 3)
    candidates = list(range(0, len(text) - width + 1, max(1, width // 2)))
    candidates.append(len(text) - width)
    selected = []
    uncovered = set(terms)
    for _ in range(3):
        available = [s for s in candidates if all(abs(s - a) >= width for a in selected)]
        if not available:
            break
        start = max(
            available,
            key=lambda s: (
                len(uncovered & _terms(text[s : s + width])),
                len(terms & _terms(text[s : s + width])),
                -s,
            ),
        )
        if not terms & _terms(text[start : start + width]):
            break
        selected.append(start)
        uncovered -= _terms(text[start : start + width])
    if not selected:
        return [(0, text[:limit])]
    return [(s, text[s : s + width]) for s in sorted(selected)]


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
