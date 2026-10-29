"""Rank candidates: eligibility, text relevance, personalization, configured priority.

Combined scoring adds bounded personal signals and configured boosts to text
relevance. Text-only scoring (`score:match`) ranks by text relevance alone.
`Ranker` prepares the query once and ranks many candidates; `rank` ranks one
catalog item.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from winlauncher.catalog.model import CatalogItem, ItemKind, fold_case, normalize
from winlauncher.catalog.priority import NO_PRIORITY, PriorityDecision, SearchPriority
from winlauncher.search.candidates import Candidate
from winlauncher.search.filters import StructuredFilters
from winlauncher.search.matching import (
    NameForms,
    best_possible_score,
    match_term,
    matches_folder_order,
    matches_path,
)
from winlauncher.search.query import MatchMode, ScoreMode, SearchQuery

APP_BONUS = 12
FAVORITE_BONUS = 80
HIGH_PRIORITY_BONUS = 20
# Learning bonuses grow per doubling of launches and are capped so repeated
# launches cannot dominate text relevance.
LAUNCH_BONUS_PER_DOUBLING = 4
LAUNCH_BONUS_CAP = 24
QUERY_LAUNCH_BONUS_PER_DOUBLING = 7
QUERY_LAUNCH_BONUS_CAP = 35
RECENCY_BONUS = 12  # Halves after RECENCY_HALF_VALUE_DAYS without use.
RECENCY_HALF_VALUE_DAYS = 7
SECONDS_PER_DAY = 86400
# Per term the path contains only with other accents than typed. Accents a user
# types are deliberate: without them a match drops by about one kind of match
# (an exact name below a prefix), while `lohn` keeps Löhne close behind Lohn.
EXTRA_ACCENTS_PENALTY = 5
MISSING_ACCENTS_PENALTY = 45
# Absorbs float rounding, so an upper bound is never a hair below the real score.
BOUND_TOLERANCE = 1e-6
_LN2 = math.log(2.0)
REASON_SEPARATOR = " · "  # Joins the reasons of one result's explanation.


class PriorityBand(IntEnum):
    """Result groups sorted before score: high priority first, low priority last."""

    PROMOTED = 0
    NORMAL = 1
    FALLBACK = 2


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A catalog item as one ranked result, with the explanation shown to the user.

    `label` replaces the item name as the displayed title, such as `%APPDATA%` for
    its folder. Actions, favorites and identity always use `item`.
    """

    item: CatalogItem
    score: float
    reason: str
    band: PriorityBand = PriorityBand.NORMAL
    label: str | None = None

    @property
    def low_priority(self) -> bool:
        return self.band == PriorityBand.FALLBACK

    @property
    def title(self) -> str:
        return self.label or self.item.name


@dataclass(frozen=True, slots=True)
class ScorePart:
    points: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Ranking:
    """How one candidate ranks; turned into a `SearchHit` only if it is displayed."""

    score: float
    band: PriorityBand
    reasons: tuple[str, ...]

    def hit(self, item: CatalogItem) -> SearchHit:
        reasons = list(self.reasons)
        if self.band == PriorityBand.FALLBACK:
            reasons.append("Low-priority fallback")
        explanation = REASON_SEPARATOR.join(dict.fromkeys(reasons)) or "Recently used"
        return SearchHit(item, self.score, explanation, self.band)


class Ranker:
    """Ranks the candidates of one query at one moment."""

    def __init__(self, query: SearchQuery, *, now: float | None = None):
        self.query = query
        self.now = time.time() if now is None else now
        self.filters = StructuredFilters(query)
        self.fuzzy = query.match_mode == MatchMode.FUZZY
        self.combined = query.score_mode == ScoreMode.COMBINED
        self.scope_search_prefix = _scope_search_prefix(query)
        # Quoted terms match as contiguous text even in fuzzy mode.
        self.fuzzy_terms = tuple(
            self.fuzzy and term not in query.exact_terms for term in query.terms
        )
        self.path_terms = tuple(matches_path(term) for term in query.terms)
        self.spellings = query.spellings or query.terms

    def eligible(self, candidate: Candidate, priority: PriorityDecision) -> bool:
        return priority.search != SearchPriority.IGNORED and self.filters.accepts(candidate)

    def band(self, priority: PriorityDecision) -> PriorityBand:
        if priority.search == SearchPriority.LOW:
            return PriorityBand.FALLBACK
        if priority.search == SearchPriority.HIGH and self.combined:
            return PriorityBand.PROMOTED
        return PriorityBand.NORMAL

    def rank(self, candidate: Candidate, priority: PriorityDecision) -> Ranking | None:
        """Rank an eligible candidate; None if its text does not match."""
        text = self.text_relevance(candidate)
        if text is None:
            return None
        if not self.combined:
            return Ranking(text.points, self.band(priority), ("Text relevance only",))
        personal = personalization(candidate, self.now)
        configured = configured_priority(priority)
        # Explicit left-to-right float addition: identical on every Python version
        # and in the Rust engine (`sum()` changed its rounding in Python 3.12).
        score = text.points + personal.points + configured.points
        reasons = text.reasons + personal.reasons + configured.reasons
        return Ranking(score, self.band(priority), reasons)

    def best_possible_score(self, candidate: Candidate, priority: PriorityDecision) -> float:
        """An upper bound for `rank(...).score`, without matching any text."""
        bound = sum(best_possible_score(term, candidate.search_name) for term in self.query.terms)
        if self.combined:
            bound += personalization(candidate, self.now).points
            bound += configured_priority(priority).points
        return bound + BOUND_TOLERANCE

    def text_relevance(self, candidate: Candidate) -> ScorePart | None:
        """Every term and the ordered folder path must match; terms add up."""
        query = self.query
        if query.folder_parts:
            folders = Path(candidate.path_text).parent.parts
            if not matches_folder_order(query.folder_parts, folders, self.fuzzy):
                return None
        if query.basename_only:
            name, path = NameForms(candidate.stem), None
        else:
            name = NameForms(candidate.name, candidate.search_name)
            path = self.matching_path(candidate)
        points, reasons = 0.0, []
        terms = zip(query.terms, self.fuzzy_terms, self.path_terms, strict=True)
        for term, fuzzy, path_term in terms:
            match = match_term(term, name, path if path_term else None, fuzzy)
            if match is None:
                return None
            points += match.score
            reasons.append(match.reason)
        if query.folder_parts:
            reasons.append("Ordered folder path")
        if penalty := self.accent_penalty(candidate):
            points -= penalty
            reasons.append("Spelled with other accents")
        return ScorePart(points, tuple(reasons))

    def accent_penalty(self, candidate: Candidate) -> int:
        """Points for terms the path contains only when accents are ignored.

        An ASCII path is spelled as typed unless the term has accents, so it needs
        no case-folded copy. Fuzzy matches are left alone: their kind of match
        already ranks them.
        """
        typed_path = None
        penalty = 0
        for term, spelling in zip(self.query.terms, self.spellings, strict=True):
            typed_accents = spelling != term
            if not typed_accents and candidate.path_text.isascii():
                continue
            if term not in candidate.search_path:
                continue
            if typed_path is None:
                typed_path = fold_case(candidate.path_text)
            if spelling not in typed_path:
                penalty += MISSING_ACCENTS_PENALTY if typed_accents else EXTRA_ACCENTS_PENALTY
        return penalty

    def matching_path(self, candidate: Candidate) -> str:
        """Explorer searches match the path below the searched folder, so the folder's
        own name does not make every descendant a "Path contains query" hit."""
        prefix = self.scope_search_prefix
        if prefix is not None and candidate.search_path.startswith(prefix):
            return candidate.search_path[len(prefix) :]
        return candidate.search_path


def _scope_search_prefix(query: SearchQuery) -> str | None:
    if not query.explorer or query.scope is None:
        return None
    return normalize(str(query.scope)).rstrip("\\/") + "\\"


def rank(
    item: CatalogItem,
    query: SearchQuery,
    priority: PriorityDecision | None = None,
    *,
    now: float | None = None,
) -> SearchHit | None:
    """Rank a single catalog item."""
    priority = priority or NO_PRIORITY
    ranker = Ranker(query, now=now)
    candidate = Candidate.from_item(item)
    if not ranker.eligible(candidate, priority):
        return None
    ranking = ranker.rank(candidate, priority)
    return ranking.hit(item) if ranking is not None else None


_NO_SIGNALS = ScorePart(0.0)
_APP_ONLY = ScorePart(float(APP_BONUS))


def personalization(candidate: Candidate, now: float) -> ScorePart:
    points = float(APP_BONUS) if candidate.kind == ItemKind.APP else 0.0
    if not (
        candidate.pinned or candidate.launches or candidate.query_launches or candidate.last_used
    ):
        # Most catalog entries were never opened; skip the arithmetic below.
        return _APP_ONLY if points else _NO_SIGNALS
    reasons = []
    if candidate.pinned:
        points += FAVORITE_BONUS
        reasons.append("Favorite")
    points += _per_doubling(candidate.launches, LAUNCH_BONUS_PER_DOUBLING, LAUNCH_BONUS_CAP)
    points += _per_doubling(
        candidate.query_launches, QUERY_LAUNCH_BONUS_PER_DOUBLING, QUERY_LAUNCH_BONUS_CAP
    )
    if candidate.query_launches:
        reasons.append("Used for this query")
    if candidate.last_used:
        age_days = max(0.0, (now - candidate.last_used) / SECONDS_PER_DAY)
        points += RECENCY_BONUS / (1 + age_days / RECENCY_HALF_VALUE_DAYS)
    return ScorePart(points, tuple(reasons))


def configured_priority(priority: PriorityDecision) -> ScorePart:
    points = float(priority.boost)
    reasons = [f"Priority boost {priority.boost:+d}"] if priority.boost else []
    if priority.search == SearchPriority.HIGH:
        points += HIGH_PRIORITY_BONUS
        reasons.append("High search priority")
    return ScorePart(points, tuple(reasons))


def _per_doubling(count: int, points: float, cap: float) -> float:
    return float(min(cap, _log2(count + 1) * points))


def _log2(value: int) -> float:
    """log2 computed identically on every Python version and in the Rust engine.

    `math.log2` rounds differently on Python 3.11 for Windows, which used its own
    fallback instead of the C library's `log2`.
    """
    mantissa, exponent = math.frexp(value)
    return math.log(2.0 * mantissa) / _LN2 + (exponent - 1)
