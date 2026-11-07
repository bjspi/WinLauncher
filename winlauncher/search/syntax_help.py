"""The search syntax as results: `?` lists the filters, `? date` only those about dates.

A help row shows a filter with an example and what it does; choosing it puts the
filter into the search field, ready to be completed. The user's own website and
plugin keywords are listed too, since those are the words nobody remembers.

Only `?` alone or followed by a space asks for help, so a wildcard search such as
`?eport` stays a search.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from winlauncher.websites import WebsiteShortcut

HELP_MARK = "?"


@dataclass(frozen=True, slots=True)
class HelpQuery:
    """Typed text asking for the search syntax, with the words that narrow it down."""

    words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SyntaxTip:
    """One help row: an example, what it does, and the text choosing it types."""

    example: str
    meaning: str
    insert: str  # Replaces the query; the user completes it.

    def matches(self, words: Iterable[str]) -> bool:
        text = f"{self.example} {self.meaning}".casefold()
        return all(word.casefold() in text for word in words)


FILTER_TIPS = (
    SyntaxTip("type:file", "Only files; also folder or app. t: is short for it", "type:"),
    SyntaxTip("ext:pdf,docx", "Only these extensions; ext:doc* with a wildcard", "ext:"),
    SyntaxTip('"exact phrase"', "Exactly this text, without initials or fuzzy matching", '"'),
    SyntaxTip('in:"D:\\Projects"', "Only inside this folder and its subfolders", 'in:"'),
    SyntaxTip("path:work/2026", "Parent folders in this order; also work\\2026\\", "path:"),
    SyntaxTip("name:report", "Match the name without its extension", "name:"),
    SyntaxTip("!draft", "Leave out paths containing this text; -draft as well", "!"),
    SyntaxTip("!ext:tmp,bak", "Leave out these extensions; !*.bak as well", "!ext:"),
    SyntaxTip("date:today", "Changed today; also yesterday, week, month, year", "date:"),
    SyntaxTip(
        "date:2026-07", "Changed in this year, month or day; 2026-01..2026-06 spans", "date:"
    ),
    SyntaxTip("after:2026-01-15", "Changed on or after this day; before: for the end", "after:"),
    SyntaxTip("size:>5mb", "Larger than 5 MB; also <, >=, <= and ranges 10mb..1gb", "size:"),
    SyntaxTip("sort:modified", "Newest first; sort:name orders by name", "sort:"),
    SyntaxTip(
        'contains:"some text"', "Files containing this text; needs the Rust engine", "contains:"
    ),
    SyntaxTip("match:keyword", "Whole words and prefixes only, no fuzzy matching", "match:"),
    SyntaxTip("score:match", "Rank by the match alone, without what you opened before", "score:"),
    SyntaxTip("low:true", "Also show results ranked as low priority", "low:true "),
    SyntaxTip("200 + 10%", "Calculator: the result shows above the results", "200 + 10%"),
)


def help_query(text: str) -> HelpQuery | None:
    """The help request in `text`; None for anything else, such as `?eport`."""
    stripped = text.strip()
    if stripped == HELP_MARK:
        return HelpQuery()
    if text.lstrip().startswith(HELP_MARK + " "):
        return HelpQuery(tuple(stripped.removeprefix(HELP_MARK).split()))
    return None


def syntax_tips(
    query: HelpQuery,
    websites: Iterable[WebsiteShortcut],
    plugin_keywords: Mapping[str, str],
) -> list[SyntaxTip]:
    """The filters, then the user's keywords, that contain every word of `query`.

    `plugin_keywords` maps each keyword to what it searches, such as "Telegram".
    """
    keyword_tips = [
        *(
            SyntaxTip(f"{site.keyword} …", f"Search {site.title}", f"{site.keyword} ")
            for site in websites
        ),
        *(
            SyntaxTip(
                f"{keyword} …", f"{label} first, then files; @{keyword} {label} only", f"{keyword} "
            )
            for keyword, label in plugin_keywords.items()
        ),
    ]
    return [tip for tip in (*FILTER_TIPS, *keyword_tips) if tip.matches(query.words)]
