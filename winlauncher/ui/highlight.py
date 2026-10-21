"""Which characters of a displayed name matched the query, for highlighting."""

from __future__ import annotations

from winlauncher.catalog.model import normalize


def matched_positions(name: str, terms: tuple[str, ...]) -> set[int]:
    """Character positions of `name` covered by the search terms.

    A term marks its first contiguous occurrence, or else its letters in order
    (as fuzzy matching would); `ö` counts as the `o` a term has. Highlighting is
    presentation only: a name with a character whose search text is not exactly
    one character, such as `ß`, is shown without highlight.
    """
    characters = [normalize(char) for char in name]
    if any(len(char) != 1 for char in characters):
        return set()
    folded = "".join(characters)
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
