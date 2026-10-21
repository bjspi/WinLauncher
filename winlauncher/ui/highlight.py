"""Which characters of a displayed name matched the query, for highlighting."""

from __future__ import annotations


def matched_positions(name: str, terms: tuple[str, ...]) -> set[int]:
    """Character positions of `name` covered by the search terms.

    A term marks its first contiguous occurrence, or else its letters in order
    (as fuzzy matching would). Highlighting is presentation only: names whose
    case folding changes their length are shown without highlight.
    """
    folded = name.casefold()
    if len(folded) != len(name):
        return set()
    positions: set[int] = set()
    for term in terms:
        positions |= _contiguous(folded, term) or _in_order(folded, term)
    return positions


def _contiguous(folded: str, term: str) -> set[int]:
    start = folded.find(term)
    if start < 0 or not term:
        return set()
    return set(range(start, start + len(term)))


def _in_order(folded: str, term: str) -> set[int]:
    positions, start = set(), 0
    for char in term:
        index = folded.find(char, start)
        if index < 0:
            return set()
        positions.add(index)
        start = index + 1
    return positions
