"""Text matching: how well one query term matches a name, path or folder.

Match qualities are tried from strongest to weakest; the first one that applies
decides the term's score.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from pathscout.catalog.model import normalize

EXACT_NAME_SCORE = 150
NAME_PREFIX_SCORE = 110
WORD_PREFIX_SCORE = 90
NAME_CONTAINS_SCORE = 75
NAME_CONTAINS_POSITION_PENALTY = 0.05  # Per character before the match.
ABBREVIATION_SCORE = 65
PATH_CONTAINS_SCORE = 35
MIN_FUZZY_TERM_LENGTH = 2

# Fuzzy alignment: per-letter rewards, averaged over the term and clamped.
FUZZY_FIRST_LETTER = 22
FUZZY_FIRST_LETTER_OFFSET_PENALTY = 0.5  # Per character before the first letter.
FUZZY_CONSECUTIVE_LETTER = 10
FUZZY_LETTER_AFTER_GAP = 4
FUZZY_GAP_PENALTY = 0.7  # Per skipped character.
FUZZY_WORD_START_BONUS = 8
FUZZY_SCORE_OFFSET = 25
FUZZY_MIN_SCORE = 5
FUZZY_MAX_SCORE = 60

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True, slots=True)
class TextMatch:
    score: float
    reason: str


class NameForms:
    """The spellings of one display name that matching compares against.

    Word forms are computed on first use: most candidates are decided by the
    cheaper exact or prefix checks.
    """

    def __init__(self, original: str):
        self.original = original
        self.normalized = normalize(original)
        self.without_extension = self.normalized.rsplit(".", 1)[0]

    @cached_property
    def words(self) -> list[str]:
        spaced = _CAMEL_CASE_BOUNDARY.sub(" ", self.original)
        return [normalize(word) for word in _WORD.findall(spaced)]

    @cached_property
    def initials(self) -> str:
        return "".join(word[0] for word in self.words)


def match_term(term: str, name: NameForms, path: Path | None, fuzzy: bool) -> TextMatch | None:
    """Score `term` against a name and, unless `path` is None, the item's full path."""
    if term in (name.normalized, name.without_extension):
        return TextMatch(EXACT_NAME_SCORE, "Exact name")
    if name.normalized.startswith(term):
        return TextMatch(NAME_PREFIX_SCORE, "Name prefix")
    if any(word.startswith(term) for word in name.words):
        return TextMatch(WORD_PREFIX_SCORE, "Word prefix")
    position = name.normalized.find(term)
    if position >= 0:
        score = NAME_CONTAINS_SCORE - position * NAME_CONTAINS_POSITION_PENALTY
        return TextMatch(score, "Name contains query")
    fuzzy_term = fuzzy and len(term) >= MIN_FUZZY_TERM_LENGTH
    if fuzzy_term and name.initials.startswith(term):
        return TextMatch(ABBREVIATION_SCORE, "Abbreviation")
    if path is not None and term in normalize(str(path)):
        return TextMatch(PATH_CONTAINS_SCORE, "Path contains query")
    if fuzzy_term and (score := fuzzy_score(term, name.original)) is not None:
        return TextMatch(score, "Fuzzy match")
    return None


def matches_folder_order(parts: tuple[str, ...], folders: tuple[str, ...], fuzzy: bool) -> bool:
    """Each part must match a parent folder below the folder the previous part matched."""
    start = 0
    for part in parts:
        position = _first_matching_folder(part, folders, start, fuzzy)
        if position is None:
            return False
        start = position + 1
    return True


def _first_matching_folder(
    part: str, folders: tuple[str, ...], start: int, fuzzy: bool
) -> int | None:
    for position in range(start, len(folders)):
        if match_term(part, NameForms(folders[position]), None, fuzzy) is not None:
            return position
    return None


def fuzzy_score(term: str, original: str) -> float | None:
    """Best in-order alignment of `term`'s letters in `original`, or None.

    Dynamic programming over (term letter, name position) in O(len(term) ×
    len(name)). Consecutive letters and word starts score higher; gaps cost a
    little per skipped character. A running maximum keeps the best earlier
    alignment, so no quadratic inner loop is needed.
    """
    name = normalize(original)
    if not term or len(term) > len(name) or not set(term).issubset(name):
        return None
    word_starts = _word_starts(original)
    impossible = float("-inf")
    previous = [impossible] * len(name)  # Best score ending at each position.
    for letter_index, letter in enumerate(term):
        current = [impossible] * len(name)
        best_before = impossible  # Best previous score, adjusted for its gap.
        for position, candidate in enumerate(name):
            if position:
                gap_adjusted = previous[position - 1] + (position - 1) * FUZZY_GAP_PENALTY
                best_before = max(best_before, gap_adjusted)
            if candidate != letter:
                continue
            bonus = FUZZY_WORD_START_BONUS if position in word_starts else 0
            if letter_index == 0:
                first = FUZZY_FIRST_LETTER - position * FUZZY_FIRST_LETTER_OFFSET_PENALTY
                current[position] = first + bonus
                continue
            after_gap = best_before - (position - 1) * FUZZY_GAP_PENALTY + FUZZY_LETTER_AFTER_GAP
            consecutive = (
                previous[position - 1] + FUZZY_CONSECUTIVE_LETTER if position else impossible
            )
            current[position] = max(after_gap, consecutive) + bonus
        previous = current
    best = max(previous)
    if not math.isfinite(best):
        return None
    average = best / len(term) + FUZZY_SCORE_OFFSET
    return max(FUZZY_MIN_SCORE, min(FUZZY_MAX_SCORE, average))


def _word_starts(original: str) -> set[int]:
    """Positions in the normalized name that start a word or a camel-case hump."""
    starts = {0}
    for index in range(1, len(original)):
        before, char = original[index - 1], original[index]
        if not before.isalnum() or (char.isupper() and before.islower()):
            starts.add(len(normalize(original[:index])))
    return starts
