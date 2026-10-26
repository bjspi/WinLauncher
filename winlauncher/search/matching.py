"""Text matching: how well one query term matches a name, path or folder.

Match qualities are tried from strongest to weakest; the first one that applies
decides the term's score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cached_property

from winlauncher.catalog.model import normalize

EXACT_NAME_SCORE = 150
NAME_PREFIX_SCORE = 110
WORD_PREFIX_SCORE = 90
NAME_CONTAINS_SCORE = 75
NAME_CONTAINS_POSITION_PENALTY = 0.05  # Per character before the match.
ABBREVIATION_SCORE = 65
PATH_CONTAINS_SCORE = 35
MIN_FUZZY_TERM_LENGTH = 2
# Plain terms shorter than this match only the name, never the folders above it:
# nearly every path contains a drive letter or some other one- or two-letter
# fragment, so "d" would otherwise find the whole catalog through "D:\".
MIN_PATH_TERM_LENGTH = 3

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
# What `_word_starts` tests letter by letter, for ASCII names: after a character that
# is no letter or digit, or a capital after a small letter.
_ASCII_WORD_START = re.compile(r"(?<=[^A-Za-z0-9])(?=.)|(?<=[a-z])(?=[A-Z])", re.DOTALL)


@dataclass(frozen=True, slots=True)
class TextMatch:
    score: float
    reason: str


class NameForms:
    """The spellings of one display name that matching compares against.

    Word forms are computed on first use: most candidates are decided by the
    cheaper exact or prefix checks.
    """

    def __init__(self, original: str, normalized: str | None = None):
        """`normalized` may come from the catalog, which stores it already."""
        self.original = original
        self.normalized = normalize(original) if normalized is None else normalized
        self.without_extension = self.normalized.rsplit(".", 1)[0]

    @cached_property
    def words(self) -> list[str]:
        spaced = _CAMEL_CASE_BOUNDARY.sub(" ", self.original)
        if spaced.isascii():  # Normalizing ASCII only lowers it; once for all words.
            return _WORD.findall(spaced.lower())
        return [normalize(word) for word in _WORD.findall(spaced)]

    @cached_property
    def initials(self) -> str:
        return "".join(word[0] for word in self.words)

    @cached_property
    def fuzzy_layout(self) -> FuzzyLayout:
        """Kept with the name, so items searched on every keystroke prepare it once."""
        return FuzzyLayout.of(self.original)

    def prepare(self) -> None:
        """Compute every spelling now rather than during the first search."""
        _ = self.initials, self.fuzzy_layout


@dataclass(frozen=True, slots=True)
class FuzzyLayout:
    """What fuzzy scoring needs to know about one name, independent of the term."""

    name: str  # Normalized.
    letters: frozenset[str]
    word_starts: frozenset[int]
    found: dict[str, tuple[int, ...]]  # Filled by `positions` for the letters typed.

    @classmethod
    def of(cls, original: str) -> FuzzyLayout:
        name = normalize(original)
        return cls(name, frozenset(name), frozenset(_word_starts(original)), {})

    def positions(self, letter: str) -> tuple[int, ...]:
        """Every position of `letter` in the name, ascending."""
        found = self.found.get(letter)
        if found is None:
            at, index = [], self.name.find(letter)
            while index >= 0:
                at.append(index)
                index = self.name.find(letter, index + 1)
            found = self.found[letter] = tuple(at)
        return found


def match_term(term: str, name: NameForms, path: str | None, fuzzy: bool) -> TextMatch | None:
    """Score `term` against a name and, unless `path` is None, the normalized full path."""
    if is_wildcard(term):
        if _wildcard_matches(term, name.normalized):
            return TextMatch(WORD_PREFIX_SCORE, "Name wildcard")
        if path is not None:
            filename = path.replace("\\", "/").rsplit("/", 1)[-1]
            if _wildcard_matches(term, filename):
                return TextMatch(WORD_PREFIX_SCORE, "Name wildcard")
            if ("/" in term or "\\" in term) and _wildcard_matches(term, path):
                return TextMatch(PATH_CONTAINS_SCORE, "Path wildcard")
        return None
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
    if path is not None and term in path:
        return TextMatch(PATH_CONTAINS_SCORE, "Path contains query")
    if fuzzy_term and (score := fuzzy_score(term, name.original, name.fuzzy_layout)) is not None:
        return TextMatch(score, "Fuzzy match")
    return None


def is_wildcard(term: str) -> bool:
    return any(char in term for char in "*?[")


def matches_path(term: str) -> bool:
    """Whether `term` may match the item's path, not only its name.

    Wildcards always may: they match whole file names, and only a pattern with a
    separator in it matches the path.
    """
    return is_wildcard(term) or len(term) >= MIN_PATH_TERM_LENGTH


def _wildcard_matches(pattern: str, text: str) -> bool:
    pattern = pattern.replace("\\", "/")
    return fnmatchcase(text.replace("\\", "/"), pattern)


def best_possible_score(term: str, normalized_name: str) -> float:
    """No match quality for `term` can score higher; used to skip hopeless candidates.

    Exact and prefix matches need the name to start with the term. Every other
    quality scores at most a word prefix.
    """
    return EXACT_NAME_SCORE if normalized_name.startswith(term) else WORD_PREFIX_SCORE


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


def fuzzy_score(term: str, original: str, layout: FuzzyLayout | None = None) -> float | None:
    """Best in-order alignment of `term`'s letters in `original`, or None.

    Dynamic programming over (term letter, name position) that visits only the
    positions holding that letter. Consecutive letters and word starts score
    higher; gaps cost a little per skipped character. A running maximum keeps the
    best earlier alignment, so no quadratic inner loop is needed. `layout` is
    `original`'s, when the caller keeps it.
    """
    layout = layout or FuzzyLayout.of(original)
    if not term or len(term) > len(layout.name) or not set(term).issubset(layout.letters):
        return None
    impossible = float("-inf")
    previous: dict[int, float] = {}  # Best score ending at each position, ascending.
    for letter_index, letter in enumerate(term):
        current: dict[int, float] = {}
        earlier = list(previous.items())
        taken = 0
        best_before = impossible  # Best previous score, adjusted for its gap.
        for position in layout.positions(letter):
            bonus = FUZZY_WORD_START_BONUS if position in layout.word_starts else 0
            if letter_index == 0:
                first = FUZZY_FIRST_LETTER - position * FUZZY_FIRST_LETTER_OFFSET_PENALTY
                current[position] = first + bonus
                continue
            while taken < len(earlier) and earlier[taken][0] < position:
                before, score = earlier[taken]
                best_before = max(best_before, score + before * FUZZY_GAP_PENALTY)
                taken += 1
            if best_before == impossible:
                continue
            after_gap = best_before - (position - 1) * FUZZY_GAP_PENALTY + FUZZY_LETTER_AFTER_GAP
            consecutive = previous.get(position - 1, impossible) + FUZZY_CONSECUTIVE_LETTER
            current[position] = max(after_gap, consecutive) + bonus
        if not current:
            return None
        previous = current
    best = max(previous.values())
    average = best / len(term) + FUZZY_SCORE_OFFSET
    return max(FUZZY_MIN_SCORE, min(FUZZY_MAX_SCORE, average))


def _word_starts(original: str) -> set[int]:
    """Positions in the normalized name that start a word or a camel-case hump."""
    if original.isascii():  # Normalizing then keeps every position.
        return {0, *(match.start() for match in _ASCII_WORD_START.finditer(original))}
    starts = {0}
    for index in range(1, len(original)):
        before, char = original[index - 1], original[index]
        if not before.isalnum() or (char.isupper() and before.islower()):
            starts.add(len(normalize(original[:index])))
    return starts
