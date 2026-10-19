"""Rank one catalog item: eligibility, text relevance, personalization, configured priority.

Combined scoring adds bounded personal signals and configured boosts to text
relevance. Text-only scoring (`score:match`) ranks by text relevance alone.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import IntEnum

from pathscout.catalog.model import CatalogItem, ItemKind
from pathscout.catalog.priority import PriorityDecision, SearchPriority
from pathscout.search.filters import passes_structured_filters
from pathscout.search.matching import NameForms, match_term, matches_folder_order
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


def rank(
    item: CatalogItem,
    query: SearchQuery,
    priority: PriorityDecision | None = None,
    *,
    now: float | None = None,
) -> SearchHit | None:
    priority = priority or PriorityDecision()
    if priority.search == SearchPriority.IGNORED:
        return None
    if not passes_structured_filters(query, item):
        return None
    text = text_relevance(item, query)
    if text is None:
        return None
    if query.score_mode == ScoreMode.MATCH:
        parts = [ScorePart(text.points, ("Text relevance only",))]
    else:
        when = time.time() if now is None else now
        parts = [text, personalization(item, when), configured_priority(priority)]
    return _hit(item, parts, _band(priority, query.score_mode))


def text_relevance(item: CatalogItem, query: SearchQuery) -> ScorePart | None:
    """Every term and the ordered folder path must match; terms add up."""
    fuzzy = query.match_mode == MatchMode.FUZZY
    if query.folder_parts:
        folders = item.path.parent.parts
        if not matches_folder_order(query.folder_parts, folders, fuzzy):
            return None
    name = NameForms(item.path.stem if query.basename_only else item.name)
    path = None if query.basename_only else item.path
    points, reasons = 0.0, []
    for term in query.terms:
        match = match_term(term, name, path, fuzzy)
        if match is None:
            return None
        points += match.score
        reasons.append(match.reason)
    if query.folder_parts:
        reasons.append("Ordered folder path")
    return ScorePart(points, tuple(reasons))


def personalization(item: CatalogItem, now: float) -> ScorePart:
    points = APP_BONUS if item.kind == ItemKind.APP else 0.0
    reasons = []
    if item.pinned:
        points += FAVORITE_BONUS
        reasons.append("Favorite")
    points += _per_doubling(item.launches, LAUNCH_BONUS_PER_DOUBLING, LAUNCH_BONUS_CAP)
    points += _per_doubling(
        item.query_launches, QUERY_LAUNCH_BONUS_PER_DOUBLING, QUERY_LAUNCH_BONUS_CAP
    )
    if item.query_launches:
        reasons.append("Used for this query")
    if item.last_used:
        age_days = max(0.0, (now - item.last_used) / SECONDS_PER_DAY)
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


def _band(priority: PriorityDecision, mode: ScoreMode) -> PriorityBand:
    if priority.search == SearchPriority.LOW:
        return PriorityBand.FALLBACK
    if priority.search == SearchPriority.HIGH and mode == ScoreMode.COMBINED:
        return PriorityBand.PROMOTED
    return PriorityBand.NORMAL


def _hit(item: CatalogItem, parts: list[ScorePart], band: PriorityBand) -> SearchHit:
    reasons = [reason for part in parts for reason in part.reasons]
    if band == PriorityBand.FALLBACK:
        reasons.append("Low-priority fallback")
    explanation = " · ".join(dict.fromkeys(reasons)) or "Recently used"
    return SearchHit(item, sum(part.points for part in parts), explanation, band)
