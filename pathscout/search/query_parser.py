"""Turn query text into a `SearchQuery`.

Tokens are separated by whitespace; double quotes group words. Backslashes and
apostrophes are literal, so Windows paths and names like "mom's" survive. A token
`key:value` is a filter only when `key` is a known filter name; any other
colon-containing token (for example `C:\\Work`) is ordinary search text.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pathscout.catalog.model import ItemKind, normalize, normalize_extension
from pathscout.search.query import MatchMode, ScoreMode, SearchQuery, SortOrder

MAX_QUERY_LENGTH = 512
MAX_TERM_LENGTH = 128
EXCLUSION_PREFIX = "!"
_Choice = TypeVar("_Choice", bound=StrEnum)
FILTER_NAMES = frozenset(
    {"type", "ext", "in", "path", "name", "match", "sort", "score", "low", "after", "before"}
)


def parse_query(text: str, scope: Path | None = None) -> SearchQuery:
    """Raise ValueError with a user-facing message for invalid input."""
    _check_raw_text(text)
    draft = _QueryDraft(scope=scope)
    for token in _split_tokens(text):
        draft.add(token)
    return draft.build()


def _check_raw_text(text: str) -> None:
    if len(text) > MAX_QUERY_LENGTH or any(ord(c) < 32 for c in text):
        raise ValueError(f"Use at most {MAX_QUERY_LENGTH} characters without control characters.")


def _split_tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.quotes = '"'
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise ValueError("Please close the quotation mark.") from exc


@dataclass
class _QueryDraft:
    """Collects tokens in order; `build` validates once and freezes the result."""

    scope: Path | None
    terms: list[str] = field(default_factory=list)
    excluded_terms: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    folder_parts: list[str] = field(default_factory=list)
    kind: ItemKind | None = None
    basename_only: bool = False
    match_mode: MatchMode = MatchMode.FUZZY
    sort: SortOrder = SortOrder.SCORE
    score_mode: ScoreMode = ScoreMode.COMBINED
    include_low_priority: bool = False
    modified_after: float | None = None
    modified_before: float | None = None

    def add(self, token: str) -> None:
        if not token:
            return  # An empty quoted string ("") is not a search term.
        name, separator, value = token.partition(":")
        if separator and normalize(name) in FILTER_NAMES:
            self._add_filter(normalize(name), value)
        elif token.startswith(EXCLUSION_PREFIX) and len(token) > len(EXCLUSION_PREFIX):
            self.excluded_terms.append(normalize(token[len(EXCLUSION_PREFIX) :]))
        else:
            self.terms.append(normalize(token))

    def _add_filter(self, name: str, value: str) -> None:
        """Each filter is one assignment; repeated list filters accumulate."""
        if name == "type":
            self.kind = _parse_choice(ItemKind, name, value)
        elif name == "ext":
            self.extensions.extend(_parse_extensions(value))
        elif name == "in":
            self.scope = _parse_scope(value)
        elif name == "path":
            self.folder_parts.extend(_parse_folder_parts(value))
        elif name == "name":
            self.basename_only = True
            if value:
                self.terms.append(normalize(value))
        elif name == "match":
            self.match_mode = _parse_choice(MatchMode, name, value)
        elif name == "sort":
            self.sort = _parse_choice(SortOrder, name, value)
        elif name == "score":
            self.score_mode = _parse_choice(ScoreMode, name, value)
        elif name == "low":
            self.include_low_priority = _parse_boolean(name, value)
        elif name == "after":
            self.modified_after = _start_of_day(_parse_day(name, value))
        elif name == "before":
            self.modified_before = _end_of_day(_parse_day(name, value))
        else:
            raise AssertionError(f"FILTER_NAMES lists {name!r} without a parser.")

    def build(self) -> SearchQuery:
        self._validate()
        return SearchQuery(
            terms=tuple(self.terms),
            kind=self.kind,
            extensions=tuple(dict.fromkeys(self.extensions)),
            scope=self.scope,
            excluded_terms=tuple(self.excluded_terms),
            folder_parts=tuple(self.folder_parts),
            basename_only=self.basename_only,
            match_mode=self.match_mode,
            sort=self.sort,
            include_low_priority=self.include_low_priority,
            modified_after=self.modified_after,
            modified_before=self.modified_before,
            score_mode=self.score_mode,
        )

    def _validate(self) -> None:
        if any(len(text) > MAX_TERM_LENGTH for text in (*self.terms, *self.folder_parts)):
            raise ValueError(
                f"Individual search terms are limited to {MAX_TERM_LENGTH} characters."
            )
        if self.modified_after is None or self.modified_before is None:
            return
        if self.modified_after >= self.modified_before:
            raise ValueError("The after date must precede the end of the before date.")


def _parse_choice(choices: type[_Choice], name: str, value: str) -> _Choice:
    try:
        return choices(normalize(value))
    except ValueError:
        expected = ", ".join(choice.value for choice in choices)
        raise ValueError(f"{name}: expects one of {expected}.") from None


def _parse_extensions(value: str) -> list[str]:
    return [normalize_extension(extension) for extension in value.split(",") if extension]


def _parse_scope(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("in: expects an absolute directory path.")
    return path


def _parse_folder_parts(value: str) -> list[str]:
    return [normalize(part) for part in value.replace("\\", "/").split("/") if part]


def _parse_boolean(name: str, value: str) -> bool:
    value = normalize(value)
    if value not in ("true", "false"):
        raise ValueError(f"{name}: expects true or false.")
    return value == "true"


def _parse_day(name: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name}: expects a date such as 2024-05-31.") from None


def _end_of_day(day: date) -> float:
    """`before:` includes the whole named day."""
    try:
        return _start_of_day(day + timedelta(days=1))
    except OverflowError:
        raise ValueError("Date is outside the supported range on this system.") from None


def _start_of_day(day: date) -> float:
    """Local midnight as a timestamp; Windows rejects dates near year 1 and 9999."""
    try:
        return datetime.combine(day, time()).timestamp()
    except (OverflowError, OSError, ValueError):
        raise ValueError("Date is outside the supported range on this system.") from None
