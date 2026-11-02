"""Match opted-in folder commands for the Explorer bar, independently of the catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from winlauncher.commands import MenuCommand
from winlauncher.search.matching import NameForms, match_term
from winlauncher.search.query import MatchMode, SearchQuery


@dataclass(frozen=True, slots=True)
class CommandHit:
    """A command bound to the searched folder; score changes preserve row identity.

    Position distinguishes separately configured commands even when they have the
    same title and command line. This is an action, never a catalog file.
    """

    command: MenuCommand
    folder: Path
    position: int
    score: float = field(compare=False)


def search_commands(
    commands: tuple[MenuCommand, ...], query: SearchQuery, folder: Path, *, fuzzy: bool = False
) -> list[CommandHit]:
    """Rank folder commands by their title and aliases; ties keep configuration order.

    The caller establishes that this is a plain text search started in Explorer.
    Each term must match a title or alias; command lines and folder paths do not
    participate in matching. Fuzzy matching is opt-in independently of the file
    query's match mode; by default only contiguous text matches.
    """
    if not query.terms:
        return []
    hits = []
    for position, command in enumerate(commands):
        if not command.explorer_search or not command.folders:
            continue
        score = _command_score(command, query, fuzzy=fuzzy)
        if score is not None:
            hits.append(CommandHit(command, folder, position, score))
    return sorted(hits, key=lambda hit: hit.score, reverse=True)


def _command_score(command: MenuCommand, query: SearchQuery, *, fuzzy: bool) -> float | None:
    """Sum each term's best title/alias match, rejecting partial multiword matches."""
    forms = [NameForms(text) for text in (command.title, *command.search_keywords)]
    allow_fuzzy = fuzzy and query.match_mode == MatchMode.FUZZY
    score = 0.0
    for term in query.terms:
        matches = [
            match.score
            for form in forms
            if (match := match_term(term, form, None, allow_fuzzy)) is not None
        ]
        if not matches:
            return None
        score += max(matches)
    return score
