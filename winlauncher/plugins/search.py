"""Which typed text plugins answer, and how their items rank against it.

Items match with the catalog's own text matching (`search.matching`), so a chat
named Anna ranks like a file named Anna. Scores share the catalog's scale: plugin
hits are merged into the file results by score.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from winlauncher.catalog.model import normalize
from winlauncher.plugins.api import PluginItem
from winlauncher.search.matching import NameForms, match_term
from winlauncher.search.query import MatchMode, SearchQuery
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import PriorityBand, SearchHit

# Plugins list their most relevant items first, such as the chats written to last.
# The first item gets this bonus; it halves every POSITION_HALF_VALUE items.
POSITION_BONUS = 12
POSITION_HALF_VALUE = 10
KEYWORD_PENALTY = 10  # A match on a keyword, such as a username, below a name match.


@dataclass(frozen=True, slots=True)
class PluginHit:
    plugin_id: str
    label: str  # The plugin's label, such as Telegram.
    item: PluginItem
    score: float
    reason: str
    band: PriorityBand = PriorityBand.NORMAL

    @property
    def title(self) -> str:
        return self.item.title or f"{self.label}: {self.item.name}"


@dataclass(frozen=True, slots=True)
class PluginQuery:
    """Typed text that starts with a plugin's keyword: only that plugin answers.

    Without further words it lists the plugin's items in its own order.
    """

    plugin_id: str
    search: SearchQuery


def plugin_query(text: str, keywords: dict[str, str]) -> PluginQuery | None:
    """The plugin query `text` asks for; `keywords` maps normalized keywords to plugin ids."""
    keyword, _, rest = text.strip().partition(" ")
    plugin_id = keywords.get(normalize(keyword)) if keyword else None
    if plugin_id is None:
        return None
    try:
        search = parse_query(rest)
    except ValueError:
        search = SearchQuery(terms=tuple(normalize(word) for word in rest.split()))
    return PluginQuery(plugin_id, search)


def plugins_answer(query: SearchQuery) -> bool:
    """Whether an ordinary search also lists plugin items.

    Only plain words do: filters for types, extensions, folders, sizes, dates or
    contents ask for files.
    """
    return bool(query.terms) and query == SearchQuery(
        terms=query.terms,
        exact_terms=query.exact_terms,
        excluded_terms=query.excluded_terms,
        match_mode=query.match_mode,
        score_mode=query.score_mode,
        include_low_priority=query.include_low_priority,
        spellings=query.spellings,
    )


def rank_items(
    plugin_id: str,
    label: str,
    items: Sequence[PluginItem],
    query: SearchQuery,
    *,
    boost: int = 0,
    limit: int,
) -> list[PluginHit]:
    """The best `limit` items for `query`; no terms keeps the plugin's order."""
    if not query.terms:
        return [
            PluginHit(plugin_id, label, item, position_bonus(position) + boost, "Recently used")
            for position, item in enumerate(items[:limit])
        ]
    fuzzy = query.match_mode == MatchMode.FUZZY
    hits = []
    for position, item in enumerate(items):
        text = _text_relevance(item, query, fuzzy)
        if text is None:
            continue
        points, reason = text
        score = points + position_bonus(position) + boost
        hits.append(PluginHit(plugin_id, label, item, score, reason))
    return heapq.nlargest(limit, hits, key=lambda hit: hit.score)


def position_bonus(position: int) -> float:
    return POSITION_BONUS / (1 + position / POSITION_HALF_VALUE)


def _text_relevance(item: PluginItem, query: SearchQuery, fuzzy: bool) -> tuple[float, str] | None:
    """Every term must match the name or a keyword; terms add up."""
    forms = [NameForms(item.name)]
    forms.extend(NameForms(keyword) for keyword in item.keywords if keyword)
    every_text = " ".join(form.normalized for form in forms)
    if any(excluded in every_text for excluded in query.excluded_terms):
        return None
    points, reasons = 0.0, []
    for term in query.terms:
        term_fuzzy = fuzzy and term not in query.exact_terms
        best = None
        for index, form in enumerate(forms):
            match = match_term(term, form, None, term_fuzzy)
            if match is None:
                continue
            score = match.score - (KEYWORD_PENALTY if index else 0)
            if best is None or score > best[0]:
                best = (score, match.reason if not index else "Keyword matches")
        if best is None:
            return None
        points += best[0]
        reasons.append(best[1])
    return points, " · ".join(dict.fromkeys(reasons))


def merge_hits(
    files: Sequence[SearchHit], plugins: Iterable[PluginHit], limit: int
) -> list[SearchHit | PluginHit]:
    """File hits keep their order; plugin hits join them by band and score."""
    ordered = sorted(plugins, key=_sort_key)
    return list(heapq.merge(files, ordered, key=_sort_key))[:limit]


def _sort_key(hit: SearchHit | PluginHit) -> tuple[int, float]:
    return hit.band, -hit.score
