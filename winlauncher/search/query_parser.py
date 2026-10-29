"""Turn query text into a `SearchQuery`.

Tokens are separated by whitespace; double quotes group words, and a quoted term
matches only as contiguous text. Backslashes and apostrophes are literal, so
Windows paths and names like "mom's" survive. A token `key:value` is a filter
only when `key` is a known filter name. Input that starts with an absolute path,
`%` or the home shortcut `~` is one path for direct navigation instead (see
`_navigation_target`). A leading `!` or `-` excludes, and a trailing separator
within a search (`reports\\`, `D:\\Work\\`) names a folder.
"""

from __future__ import annotations

import ntpath
import re
from calendar import monthrange
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from fractions import Fraction
from math import ceil, floor
from pathlib import Path, PureWindowsPath
from typing import TypeVar

from winlauncher.catalog.model import (
    FOLDER_SEPARATORS,
    ItemKind,
    fold_case,
    normalize,
    normalize_extension,
)
from winlauncher.search.query import MatchMode, ScoreMode, SearchQuery, SortOrder

MAX_QUERY_LENGTH = 512
MAX_TERM_LENGTH = 128
EXCLUSION_PREFIXES = ("!", "-")
RANGE_SEPARATOR = ".."
_Choice = TypeVar("_Choice", bound=StrEnum)
_Bound = TypeVar("_Bound", int, float)
FILTER_NAMES = frozenset(
    {
        "type",
        "t",
        "ext",
        "in",
        "path",
        "name",
        "match",
        "sort",
        "score",
        "low",
        "after",
        "before",
        "date",
        "size",
        "contains",
        "content",
        "rg",
    }
)
CONTENT_FILTERS = frozenset({"contains", "content", "rg"})
_HOME_SHORTCUT = "~"
_NAVIGATION_PREFIXES = ("%", "~/", "~\\")
# Win32 device namespaces (`\\.\pipe\`, `\\?\C:\`) are absolute but no user paths.
_DEVICE_PREFIXES = ("\\\\.\\", "\\\\?\\", "//./", "//?/")
_QUOTE = '"'
# Whitespace separates tokens except inside double quotes; an open quote runs to the end.
_TOKEN = re.compile(r'(?:"[^"]*"?|[^ \t\r\n"])+')

_LIST_SEPARATOR = re.compile(r"[,;]")
_EXTENSION_FILTERS = frozenset({"ext", *(prefix + "ext" for prefix in EXCLUSION_PREFIXES)})
# `!*.bak` excludes one extension; wildcards, dots or separators keep it a literal exclusion.
_EXTENSION_SHORTHAND = re.compile(r"\*\.([^*?\[./\\]+)")

_SIZE_UNITS = {"": 1, "b": 1, "byte": 1, "bytes": 1} | {
    prefix + suffix: 1024**power
    for power, prefix in enumerate("kmgt", 1)
    for suffix in ("b", "byte", "bytes", "ib")
}
_MAX_SIZE = 2**63 - 1  # SQLite and the native engine store signed 64-bit sizes.
_SIZE_COMPARISONS = (">", "<", ">=", "<=")
_SIZE_AMOUNT = r"([0-9]+(?:[.,][0-9]+)?)\s*([a-z]*)"
_SIZE_COMPARISON = re.compile(rf"(>=|<=|>|<)\s*{_SIZE_AMOUNT}")
_SIZE_RANGE = re.compile(rf"{_SIZE_AMOUNT}{re.escape(RANGE_SEPARATOR)}{_SIZE_AMOUNT}")
_SIZE_HELP = (
    "size: expects >5kb, <2mb, >=1gb, <=512kbyte or a range such as 10mb..1gb (b/kb/mb/gb/tb)."
)

_PARTIAL_DATE = re.compile(r"[0-9]{4}(?:-[0-9]{2}){0,2}")
_DATE_COMPARISON = re.compile(r"(>=|<=|>|<)(.*)")
# `date:` periods that end today, in calendar days including today.
DAYS_IN_RECENT_WEEK = 7
DAYS_IN_RECENT_MONTH = 30
DAYS_IN_RECENT_YEAR = 365
_RECENT_PERIOD_DAYS = {
    "today": 1,
    "week": DAYS_IN_RECENT_WEEK,
    "month": DAYS_IN_RECENT_MONTH,
    "year": DAYS_IN_RECENT_YEAR,
}
_DATE_HELP = (
    "date: expects today, yesterday, week, month, year, a date such as 2026, 2026-07 "
    "or 2026-07-15, a range such as 2026-01..2026-06 or a comparison such as >=2026-03."
)


class SpanRole(StrEnum):
    FILTER = "filter"
    CONTENT = "content"
    EXCLUSION = "exclusion"


@dataclass(frozen=True, slots=True)
class QuerySpan:
    """Characters `start:end` of the typed text form a filter or an exclusion.

    `valid` is False when `parse_query` would reject the token.
    """

    start: int
    end: int
    role: SpanRole
    valid: bool = True


@dataclass(frozen=True, slots=True)
class _Token:
    text: str  # Without the quotation marks.
    start: int
    end: int
    closed: bool = True
    quoted: bool = False  # Written with quotation marks: a plain term then matches exactly.

    def joined(self, other: _Token, separator: str = "") -> _Token:
        return _Token(
            self.text + separator + other.text,
            self.start,
            other.end,
            self.closed and other.closed,
            self.quoted or other.quoted,
        )


def parse_query(text: str, scope: Path | None = None, *, today: date | None = None) -> SearchQuery:
    """Raise ValueError with a user-facing message for invalid input.

    `today` anchors relative dates such as `date:week`; it defaults to the local date.
    """
    _check_raw_text(text)
    if (target := _navigation_target(text)) is not None:
        return SearchQuery(navigation=target)
    draft = _QueryDraft(scope=scope, today=today or date.today())
    for token in _query_tokens(text):
        if not token.closed:
            raise ValueError("Please close the quotation mark.")
        draft.add(token.text, quoted=token.quoted)
    return draft.build()


def query_spans(text: str) -> tuple[QuerySpan, ...]:
    """Filters and exclusions in `text`, classified as `parse_query` reads them.

    Presentation only: never raises, so text being typed can be highlighted.
    """
    if _navigation_target(text) is not None:
        return ()
    spans = (_span(token) for token in _query_tokens(text))
    return tuple(span for span in spans if span is not None)


def _navigation_target(text: str) -> str | None:
    """The path that `text` navigates to directly, or None for an ordinary search.

    The whole trimmed input is one path, spaces and filter-like text included, when
    it starts with `%`, is `~` or starts with `~/`, or is an absolute drive or UNC
    path. One pair of quotes around an absolute path is removed, as Explorer's
    "Copy as path" adds them. Purely syntactic: the filesystem is not touched.
    """
    stripped = text.strip()
    unquoted = _without_enclosing_quotes(stripped)
    if unquoted is not None and _is_absolute_path(unquoted):
        return unquoted
    if stripped == _HOME_SHORTCUT or stripped.startswith(_NAVIGATION_PREFIXES):
        return stripped
    return stripped if _is_absolute_path(stripped) else None


def _without_enclosing_quotes(text: str) -> str | None:
    """The text inside one pair of quotes that encloses all of `text`, if it does."""
    inner = text[1:-1]
    enclosed = len(text) > 1 and text[0] == text[-1] == _QUOTE and _QUOTE not in inner
    return inner if enclosed else None


def _is_absolute_path(text: str) -> bool:
    r"""A drive path (`C:\`) or UNC share (`\\server\share`), not a device path.

    Drive and root are checked instead of `is_absolute()`, which since Python 3.13
    also accepts an incomplete UNC server name such as `\\server`.
    """
    path = PureWindowsPath(text)
    return bool(path.drive and path.root) and not text.startswith(_DEVICE_PREFIXES)


def _query_tokens(text: str) -> Iterator[_Token]:
    """Tokens with `size: > 5kb` and `ext:(jpg, png)` joined into one filter each."""
    tokens = _scan_tokens(text)
    for token in tokens:
        name, separator, value = token.text.partition(":")
        if separator and normalize(name) == "size":
            if not value and (following := next(tokens, None)):
                token, value = token.joined(following), following.text
            if value in _SIZE_COMPARISONS and (following := next(tokens, None)):
                token = token.joined(following)
        elif separator and normalize(name) in _EXTENSION_FILTERS and value.startswith("("):
            while not token.text.endswith(")"):
                following = next(tokens, None)
                if following is None:
                    break  # `ext:` itself reports the missing parenthesis.
                token = token.joined(following, " ")
        yield token


def _scan_tokens(text: str) -> Iterator[_Token]:
    for match in _TOKEN.finditer(text):
        raw = match.group()
        quotes = raw.count('"')
        yield _Token(raw.replace('"', ""), match.start(), match.end(), quotes % 2 == 0, quotes > 0)


def _span(token: _Token) -> QuerySpan | None:
    text = token.text
    if (name := _filter_name(text)) is not None:
        role = SpanRole.CONTENT if name in CONTENT_FILTERS else SpanRole.FILTER
    elif _is_exclusion(text):
        role = SpanRole.EXCLUSION
    elif _is_folder_shorthand(text):
        role = SpanRole.FILTER
    else:
        return None
    return QuerySpan(token.start, token.end, role, _is_valid(token))


def _is_valid(token: _Token) -> bool:
    """Whether `parse_query` would accept this token on its own."""
    if not token.closed:
        return False
    draft = _QueryDraft(scope=None, today=date.today())
    try:
        draft.add(token.text, quoted=token.quoted)
        draft.build()
    except (ValueError, RuntimeError):  # RuntimeError: `~` without a home directory.
        return False
    return True


def _filter_name(token: str) -> str | None:
    """The normalized filter name of a `name:value` token; None for other text."""
    name, separator, _ = token.partition(":")
    name = normalize(name)
    if separator and name in FILTER_NAMES and not ntpath.isabs(token):
        return name
    return None


def _is_exclusion(token: str) -> bool:
    """`!old` and `-old` exclude; a lone `!` or `-` and `my-file` are ordinary text."""
    return token.startswith(EXCLUSION_PREFIXES) and len(token) > 1


def _is_folder_shorthand(token: str) -> bool:
    """`reports\\` and `D:\\Work\\` name a folder; bare separators are ordinary text."""
    return token.endswith(FOLDER_SEPARATORS) and bool(token.strip("".join(FOLDER_SEPARATORS)))


def _check_raw_text(text: str) -> None:
    if len(text) > MAX_QUERY_LENGTH or any(ord(c) < 32 for c in text):
        raise ValueError(f"Use at most {MAX_QUERY_LENGTH} characters without control characters.")


def without_type_filter(text: str) -> str:
    """Remove typed kind overrides when a user selects a different UI filter."""
    if _navigation_target(text) is not None:
        return text
    tokens = _split_tokens(text)
    kept = [
        token
        for token in tokens
        if normalize(token.partition(":")[0]) not in ("type", "t")
        or ":" not in token
        or ntpath.isabs(token)
    ]
    if len(kept) == len(tokens):
        return text
    return " ".join(
        f'"{token}"' if not token or any(c.isspace() for c in token) else token for token in kept
    )


def _split_tokens(text: str) -> list[str]:
    tokens = list(_scan_tokens(text))
    if not all(token.closed for token in tokens):
        raise ValueError("Please close the quotation mark.")
    return [token.text for token in tokens]


@dataclass
class _QueryDraft:
    """Collects tokens in order; `build` validates once and freezes the result."""

    scope: Path | None
    today: date  # Anchors relative dates.
    terms: list[str] = field(default_factory=list)
    exact_terms: list[str] = field(default_factory=list)
    excluded_terms: list[str] = field(default_factory=list)
    excluded_extensions: list[str] = field(default_factory=list)
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
    min_size: int | None = None
    max_size: int | None = None
    content_text: str | None = None
    spellings: list[str] = field(default_factory=list)

    def add_term(self, value: str) -> str:
        """Search text for `value`; its spelling ranks results written the same way higher."""
        term = normalize(value)
        self.terms.append(term)
        self.spellings.append(fold_case(value))
        return term

    def add(self, token: str, *, quoted: bool = False) -> None:
        """`quoted` makes an ordinary term exact; filters and exclusions ignore it."""
        if not token:
            return  # An empty quoted string ("") is not a search term.
        if (name := _filter_name(token)) is not None:
            self.add_filter(name, token.partition(":")[2])
        elif _is_exclusion(token):
            self.add_exclusion(token[1:])
        elif _is_folder_shorthand(token):
            self.add_folder(token)
        else:
            term = self.add_term(token)
            if quoted:
                self.exact_terms.append(term)

    def add_exclusion(self, text: str) -> None:
        """`ext:` lists and `*.bak` exclude extensions; other text is a path substring."""
        name, separator, value = text.partition(":")
        if separator and normalize(name) == "ext":
            self.excluded_extensions.extend(_parse_extensions(value))
        elif shorthand := _EXTENSION_SHORTHAND.fullmatch(text):
            self.excluded_extensions.append(normalize_extension(shorthand[1]))
        else:
            self.excluded_terms.append(normalize(text))

    def add_folder(self, text: str) -> None:
        """An absolute folder scopes like `in:`; a relative one adds `path:` parts."""
        path = _expanded_path(text)
        if path.is_absolute():
            self.scope = path
        else:
            self.folder_parts.extend(_parse_folder_parts(text))

    def add_filter(self, name: str, value: str) -> None:
        """Each filter is one assignment; repeated list filters accumulate and
        size/date bounds intersect."""
        if name in ("type", "t"):
            self.kind = _parse_choice(
                ItemKind, name, "folder" if normalize(value) == "dir" else value
            )
        elif name == "ext":
            self.extensions.extend(_parse_extensions(value))
        elif name == "in":
            self.scope = _parse_scope(value)
        elif name == "path":
            self.folder_parts.extend(_parse_folder_parts(value))
        elif name == "name":
            self.basename_only = True
            if value:
                self.add_term(value)
        elif name == "match":
            self.match_mode = _parse_choice(MatchMode, name, value)
        elif name == "sort":
            self.sort = _parse_choice(SortOrder, name, value)
        elif name == "score":
            self.score_mode = _parse_choice(ScoreMode, name, value)
        elif name == "low":
            self.include_low_priority = _parse_boolean(name, value)
        elif name == "after":
            self.restrict_modified(_start_of_day(_parse_day(name, value)[0]), None)
        elif name == "before":
            self.restrict_modified(None, _end_of_day(_parse_day(name, value)[1]))
        elif name == "date":
            self.restrict_modified(*_parse_date_filter(value, self.today))
        elif name == "size":
            lower, upper = _parse_size(value)
            self.min_size = _narrowed(self.min_size, lower, max)
            self.max_size = _narrowed(self.max_size, upper, min)
        elif name in CONTENT_FILTERS:
            if not value.strip() or len(value) > MAX_TERM_LENGTH:
                raise ValueError(f"{name}: expects 1 to {MAX_TERM_LENGTH} characters of text.")
            self.content_text = value
        else:
            raise AssertionError(f"FILTER_NAMES lists {name!r} without a parser.")

    def restrict_modified(self, after: float | None, before: float | None) -> None:
        """Date filters intersect: the latest start and the earliest end apply."""
        self.modified_after = _narrowed(self.modified_after, after, max)
        self.modified_before = _narrowed(self.modified_before, before, min)

    def build(self) -> SearchQuery:
        self._validate()
        return SearchQuery(
            terms=tuple(self.terms),
            exact_terms=tuple(dict.fromkeys(self.exact_terms)),
            kind=self.kind,
            extensions=tuple(dict.fromkeys(self.extensions)),
            scope=self.scope,
            excluded_terms=tuple(self.excluded_terms),
            excluded_extensions=tuple(dict.fromkeys(self.excluded_extensions)),
            folder_parts=tuple(self.folder_parts),
            basename_only=self.basename_only,
            match_mode=self.match_mode,
            sort=self.sort,
            include_low_priority=self.include_low_priority,
            modified_after=self.modified_after,
            modified_before=self.modified_before,
            min_size=self.min_size,
            max_size=self.max_size,
            content_text=self.content_text,
            score_mode=self.score_mode,
            spellings=tuple(self.spellings),
        )

    def _validate(self) -> None:
        if any(len(text) > MAX_TERM_LENGTH for text in (*self.terms, *self.folder_parts)):
            raise ValueError(
                f"Individual search terms are limited to {MAX_TERM_LENGTH} characters."
            )
        if (
            self.min_size is not None
            and self.max_size is not None
            and self.min_size > self.max_size
        ):
            raise ValueError("The size filters describe an empty range.")
        if (
            self.modified_after is not None
            and self.modified_before is not None
            and self.modified_after >= self.modified_before
        ):
            raise ValueError("The date filters describe an empty range.")


def _narrowed(
    current: _Bound | None, bound: _Bound | None, tighter: Callable[[_Bound, _Bound], _Bound]
) -> _Bound | None:
    """`current` limited by `bound`, where None means unbounded."""
    if bound is None:
        return current
    return bound if current is None else tighter(current, bound)


def _parse_choice(choices: type[_Choice], name: str, value: str) -> _Choice:
    try:
        return choices(normalize(value))
    except ValueError:
        expected = ", ".join(choice.value for choice in choices)
        raise ValueError(f"{name}: expects one of {expected}.") from None


def _parse_extensions(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("("):
        if not value.endswith(")"):
            raise ValueError("Please close the ext: list with a parenthesis.")
        value = value[1:-1]
    extensions = [
        normalize_extension(extension.strip())
        for extension in _LIST_SEPARATOR.split(value)
        if extension.strip()
    ]
    if not extensions:
        raise ValueError(
            "ext: expects an extension or list such as doc*, doc,docx, pdf;txt or (jpg,png)."
        )
    return extensions


def _parse_scope(value: str) -> Path:
    path = _expanded_path(value)
    if not path.is_absolute():
        raise ValueError("in: expects an absolute directory path.")
    return path


def _expanded_path(value: str) -> Path:
    """The folder `value` names, with `%VARIABLE%` and `~` expanded."""
    return Path(ntpath.expandvars(value)).expanduser()


def _parse_folder_parts(value: str) -> list[str]:
    return [normalize(part) for part in value.replace("\\", "/").split("/") if part]


def _parse_boolean(name: str, value: str) -> bool:
    value = normalize(value)
    if value not in ("true", "false"):
        raise ValueError(f"{name}: expects true or false.")
    return value == "true"


def _parse_day(name: str, value: str) -> tuple[date, date]:
    """The first and last day that an `after:` or `before:` value names."""
    try:
        return _parse_period(value)
    except ValueError:
        raise ValueError(f"{name}: expects a date such as 2026, 2026-02* or 2026-02-28.") from None


def _parse_period(value: str) -> tuple[date, date]:
    """The first and last day of `YYYY`, `YYYY-MM` or `YYYY-MM-DD` (optionally ending
    in `*` or `-`), or of any other ISO 8601 day.

    Raises ValueError without a user-facing message; callers name their filter.
    """
    expanded = value.removesuffix("*").removesuffix("-")
    if not _PARTIAL_DATE.fullmatch(expanded):
        day = date.fromisoformat(value)
        return day, day
    year, *rest = (int(part) for part in expanded.split("-"))
    if not rest:
        return date(year, 1, 1), date(year, 12, 31)
    if len(rest) == 1:
        first = date(year, rest[0], 1)
        return first, first.replace(day=monthrange(year, first.month)[1])
    day = date(year, *rest)
    return day, day


def _parse_date_filter(value: str, today: date) -> tuple[float | None, float | None]:
    """`date:` as local timestamps `[after, before)`; None leaves that side open."""
    relative = normalize(value)
    if relative in _RECENT_PERIOD_DAYS:
        first = today - timedelta(days=_RECENT_PERIOD_DAYS[relative] - 1)
        return _start_of_day(first), None
    if relative == "yesterday":
        return _start_of_day(today - timedelta(days=1)), _start_of_day(today)
    try:
        if comparison := _DATE_COMPARISON.fullmatch(value):
            operator = comparison[1]
            first, last = _parse_period(comparison[2])
        elif RANGE_SEPARATOR in value:
            start, _, end = value.partition(RANGE_SEPARATOR)
            operator = RANGE_SEPARATOR
            first, last = _parse_period(start)[0], _parse_period(end)[1]
        else:
            operator = ""
            first, last = _parse_period(value)
    except ValueError:
        raise ValueError(_DATE_HELP) from None
    if operator == ">":
        return _end_of_day(last), None
    if operator == ">=":
        return _start_of_day(first), None
    if operator == "<":
        return None, _start_of_day(first)
    if operator == "<=":
        return None, _end_of_day(last)
    return _start_of_day(first), _end_of_day(last)  # A period or an inclusive range.


def _parse_size(value: str) -> tuple[int | None, int | None]:
    """Inclusive byte bounds: one side for a comparison, both for an `a..b` range."""
    text = value.lower()
    if size_range := _SIZE_RANGE.fullmatch(text):
        lower = _size_bound(">=", size_range[1], size_range[2])
        return lower, _size_bound("<=", size_range[3], size_range[4])
    comparison = _SIZE_COMPARISON.fullmatch(text)
    if comparison is None:
        raise ValueError(_SIZE_HELP)
    bound = _size_bound(comparison[1], comparison[2], comparison[3])
    return (bound, None) if comparison[1].startswith(">") else (None, bound)


def _size_bound(comparison: str, number: str, unit: str) -> int:
    """The whole-byte inclusive bound of `comparison` with this amount."""
    if unit not in _SIZE_UNITS:
        raise ValueError(_SIZE_HELP)
    amount = Fraction(number.replace(",", ".")) * _SIZE_UNITS[unit]
    if comparison == ">":
        bound = floor(amount) + 1
    elif comparison == ">=":
        bound = ceil(amount)
    elif comparison == "<":
        bound = ceil(amount) - 1
    else:
        bound = floor(amount)
    if not 0 <= bound <= _MAX_SIZE:
        raise ValueError(
            "size: must describe a nonnegative size within the signed 64-bit byte range."
        )
    return bound


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
