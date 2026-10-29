"""Which typed text plugins answer, and how their items rank against it.

Items match with the catalog's own text matching (`search.matching`), so a chat
named Anna ranks like a file named Anna. Scores share the catalog's scale: plugin
hits are merged into the file results by score.
"""

from __future__ import annotations

import heapq
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

from winlauncher.catalog.model import normalize
from winlauncher.plugins.api import PluginItem
from winlauncher.search.matching import (
    MIN_FUZZY_TERM_LENGTH,
    NameForms,
    is_wildcard,
    match_term,
)
from winlauncher.search.query import MatchMode, SearchQuery
from winlauncher.search.query_parser import parse_query
from winlauncher.search.ranking import REASON_SEPARATOR, PriorityBand, SearchHit

# Plugins list their most relevant items first, such as the chats written to last.
# The first item gets this bonus; it halves every POSITION_HALF_VALUE items.
POSITION_BONUS = 12
POSITION_HALF_VALUE = 10
KEYWORD_PENALTY = 10  # A match on a keyword, such as a username, below a name match.
# Typed before a keyword, it asks for that plugin alone: `@tg anna` searches only
# Telegram, while `tg anna` searches it and the catalog, so a short keyword never
# stops a file named "tg" from being found.
ONLY_PLUGIN_PREFIX = "@"
# A term with a digit in it names something: a ticket, a version, a phone number. Its
# characters are matched in one piece, so `12345` never finds the ticket 52314, while
# the words of the same query keep matching loosely.
_HAS_DIGIT = re.compile(r"\d")
# Ranking runs in Python on a search worker, and Python runs one thread at a time.
# Give the GUI a real turn after this much filtering and matching work; sleep(0)
# can immediately reacquire the interpreter and starve Qt's Python callbacks.
CHECKPOINT_INTERVAL_S = 0.005
CHECKPOINT_PAUSE_S = 0.001


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
class KeywordTarget:
    """What a keyword searches: one plugin, or only the items of one of its routes."""

    plugin_id: str
    route: str | None = None


@dataclass(frozen=True, slots=True)
class PluginQuery:
    """Typed text that starts with a plugin's keyword.

    The plugin searches the words behind its keyword, and without any it lists its
    items in its own order; a route's keyword keeps to that route's items. `files`
    is the catalog search the same text also runs, so a keyword never takes a word
    away from the ordinary search; `@keyword` asks for that plugin alone and leaves
    it None.
    """

    plugin_id: str
    search: SearchQuery
    files: SearchQuery | None = None
    route: str | None = None


def plugin_query(text: str, keywords: dict[str, KeywordTarget]) -> PluginQuery | None:
    """The plugin query `text` asks for; `keywords` maps normalized keywords to targets."""
    typed = text.strip()
    alone = typed.startswith(ONLY_PLUGIN_PREFIX)
    keyword, _, rest = typed.removeprefix(ONLY_PLUGIN_PREFIX).partition(" ")
    target = keywords.get(normalize(keyword)) if keyword else None
    if target is None:
        return None
    files = None if alone else _parsed(typed)
    return PluginQuery(target.plugin_id, _parsed(rest), files, target.route)


def _parsed(text: str) -> SearchQuery:
    """`text` as a search; unparsable text, such as an unclosed quote, stays words."""
    try:
        return parse_query(text)
    except ValueError:
        return SearchQuery(terms=tuple(normalize(word) for word in text.split()))


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


@dataclass(frozen=True, slots=True)
class SearchableItem:
    """A plugin item together with the spellings matching compares against.

    Normalizing text is the expensive half of matching, and an item's text changes
    only when its plugin refreshes. It is therefore prepared there, not on every
    keystroke, exactly as the catalog stores normalized search text per entry.
    """

    item: PluginItem
    position: int  # Where the plugin listed it; the candidate test may skip items.
    forms: tuple[NameForms, ...]  # The name first, then every keyword.
    searchable: str  # All of their normalized text, for the candidate test.


def searchable_items(
    items: Iterable[PluginItem], *, prepare: bool = True
) -> tuple[SearchableItem, ...]:
    """Prepare a plugin's items for searching; called once per refresh.

    `prepare` also computes the word forms fuzzy matching needs, which otherwise
    the first search after each refresh would; see `prepare_forms`.
    """
    prepared = []
    for position, item in enumerate(items):
        forms = (NameForms(item.name), *(NameForms(word) for word in item.keywords if word))
        text = " ".join(form.normalized for form in forms)
        prepared.append(SearchableItem(item, position, forms, text))
    result = tuple(prepared)
    if prepare:
        prepare_forms(result)
    return result


def prepare_forms(items: Iterable[SearchableItem]) -> None:
    """Compute the word forms of `items` now, on a worker, rather than while typing."""
    for entry in items:
        for form in entry.forms:
            form.prepare()


def literal_patterns(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    r"""Compile a plugin's `literal_terms`; raises `re.error` for an unusable one.

    Terms reach matching normalized, so the patterns ignore case to let a plugin
    write them the way its identifiers look, such as `HCP-\w*`.
    """
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def rank_items(
    plugin_id: str,
    label: str,
    items: Sequence[SearchableItem],
    query: SearchQuery,
    *,
    boost: int = 0,
    limit: int,
    literal: Sequence[re.Pattern[str]] = (),
    cancel: threading.Event | None = None,
) -> list[PluginHit]:
    """The best `limit` items for `query`; no terms keeps the plugin's order.

    `literal` are the plugin's patterns for terms that name one of its items, such
    as a ticket key, and must therefore be matched in one piece. A set `cancel`
    stops the ranking at its next checkpoint and leaves no hits.
    """
    if cancel is not None and cancel.is_set():
        return []
    if not query.terms:
        return [
            PluginHit(
                plugin_id,
                label,
                entry.item,
                position_bonus(entry.position) + boost,
                "Recently used",
            )
            for entry in items[:limit]
        ]
    loose = _loose_terms(query, literal)
    hits = []
    for entry in _candidates(items, query, loose, cancel):
        text = _text_relevance(entry, query, loose)
        if text is None:
            continue
        points, reason = text
        score = points + position_bonus(entry.position) + boost
        hits.append(PluginHit(plugin_id, label, entry.item, score, reason))
    if cancel is not None and cancel.is_set():
        return []
    return heapq.nlargest(limit, hits, key=lambda hit: hit.score)


def position_bonus(position: int) -> float:
    return POSITION_BONUS / (1 + position / POSITION_HALF_VALUE)


def _loose_terms(query: SearchQuery, literal: Sequence[re.Pattern[str]]) -> frozenset[str]:
    """The terms that may match loosely; decided once per search, not per item.

    A term is matched in one piece when the user asked for it (quoted or
    `match:keyword`), when it contains a digit, or when the plugin declared a
    pattern that describes the whole term.
    """
    if query.match_mode != MatchMode.FUZZY:
        return frozenset()
    return frozenset(
        term
        for term in query.terms
        if term not in query.exact_terms
        and not _HAS_DIGIT.search(term)
        and not any(pattern.fullmatch(term) for pattern in literal)
    )


def _candidates(
    items: Sequence[SearchableItem],
    query: SearchQuery,
    loose: frozenset[str],
    cancel: threading.Event | None = None,
) -> Iterator[SearchableItem]:
    """The items that can possibly match, decided by one cheap test per term.

    The catalog never ranks every entry either: its candidate SQL asks for
    `LIKE '%term%'`, and for the fuzzy pass for the subsequence `'%a%b%c%'`, so
    only those rows reach the Python matcher. Here the same two tests are a
    substring check and successive `str.find` calls, without regex backtracking.
    Filtering streams into matching, sharing its timed checkpoints even when no
    items match. Every test stays a superset of `match_term`.
    """
    tests = []
    for term in query.terms:
        test = _candidate_test(term, term in loose)
        if test is not None:
            tests.append(test)
    deadline = time.perf_counter() + CHECKPOINT_INTERVAL_S
    for entry in items:
        if time.perf_counter() >= deadline:
            time.sleep(CHECKPOINT_PAUSE_S)
            deadline = time.perf_counter() + CHECKPOINT_INTERVAL_S
        if cancel is not None and cancel.is_set():
            return
        if all(test(entry.searchable) for test in tests):
            yield entry


def _candidate_test(term: str, fuzzy: bool) -> Callable[[str], bool] | None:
    """How `term` can be ruled out cheaply, or None when it cannot be."""
    if is_wildcard(term):
        return None
    if not fuzzy or len(term) < MIN_FUZZY_TERM_LENGTH:
        return lambda text: term in text

    def in_order(text: str) -> bool:
        """Find each letter once; failed alignments never need to be retried."""
        start = 0
        for letter in term:
            position = text.find(letter, start)
            if position < 0:
                return False
            start = position + 1
        return True

    return in_order


def _text_relevance(
    entry: SearchableItem, query: SearchQuery, loose: frozenset[str]
) -> tuple[float, str] | None:
    """Every term must match the name or a keyword; terms add up."""
    if any(excluded in entry.searchable for excluded in query.excluded_terms):
        return None
    points, reasons = 0.0, []
    for term in query.terms:
        term_fuzzy = term in loose
        best = None
        for index, form in enumerate(entry.forms):
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
    return points, REASON_SEPARATOR.join(dict.fromkeys(reasons))


def merge_hits(
    files: Sequence[SearchHit], plugins: Iterable[PluginHit], limit: int
) -> list[SearchHit | PluginHit]:
    """File hits keep their order; plugin hits join them by band and score."""
    ordered = sorted(plugins, key=_sort_key)
    return list(heapq.merge(files, ordered, key=_sort_key))[:limit]


def _sort_key(hit: SearchHit | PluginHit) -> tuple[int, float]:
    return hit.band, -hit.score
