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

from pathscout.catalog.model import CatalogItem, ItemKind
from pathscout.catalog.priority import NO_PRIORITY, PriorityDecision, SearchPriority
from pathscout.search.candidates import Candidate
from pathscout.search.filters import StructuredFilters
from pathscout.search.matching import (
    NameForms,
    best_possible_score,
    match_term,
    matches_folder_order,
)
from pathscout.search.query import MatchMode, ScoreMode, SearchQuery

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
# Absorbs float rounding, so an upper bound is never a hair below the real score.
BOUND_TOLERANCE = 1e-6


class PriorityBand(IntEnum):
    """Result groups sorted before score: high priority first, low priority last."""

    PROMOTED = 0
    NORMAL = 1
    FALLBACK = 2


@dataclass(frozen=True, slots=True)
class SearchHit:
    item: CatalogItem
    score: float
    reason: str
    band: PriorityBand = PriorityBand.NORMAL

    @property
    def low_priority(self) -> bool:
        return self.band == PriorityBand.FALLBACK


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
        explanation = " · ".join(dict.fromkeys(reasons)) or "Recently used"
        return SearchHit(item, self.score, explanation, self.band)


class Ranker:
    """Ranks the candidates of one query at one moment."""

    def __init__(self, query: SearchQuery, *, now: float | None = None):
        self.query = query
        self.now = time.time() if now is None else now
        self.filters = StructuredFilters(query)
        self.fuzzy = query.match_mode == MatchMode.FUZZY
        self.combined = query.score_mode == ScoreMode.COMBINED

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
        if self.combined:
            parts = [text, personalization(candidate, self.now), configured_priority(priority)]
        else:
            parts = [ScorePart(text.points, ("Text relevance only",))]
        reasons = tuple(reason for part in parts for reason in part.reasons)
        return Ranking(sum(part.points for part in parts), self.band(priority), reasons)

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
            path = None if query.explorer else candidate.search_path
        points, reasons = 0.0, []
        for term in query.terms:
            match = match_term(term, name, path, self.fuzzy)
            if match is None:
                return None
            points += match.score
            reasons.append(match.reason)
        if query.folder_parts:
            reasons.append("Ordered folder path")
        return ScorePart(points, tuple(reasons))


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
    points = APP_BONUS if candidate.kind == ItemKind.APP else 0.0
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
    return min(cap, math.log2(count + 1) * points)
