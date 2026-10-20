"""Configured website keywords and URL templates; no browser or GUI access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from pathscout.catalog.model import normalize
from pathscout.search.query import SearchQuery
from pathscout.search.query_parser import parse_query

PLACEHOLDER = "%s"  # Where a URL template receives the search text.
_KEYWORD = re.compile(r"[\w-]+")


def website_origin(url: str) -> str:
    """Validate an HTTP(S) URL and return its origin without path or search terms."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Website URLs must not contain spaces or control characters.")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme in ("https", "http")
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and "\\" not in url
            and PLACEHOLDER not in parts.netloc
        )
        _ = parts.port  # Validate a supplied port, too.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use an http:// or https:// website URL without login credentials.")
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


@dataclass(frozen=True, slots=True)
class WebsiteTarget:
    """A page to open in the browser: a search on a website, or the website itself."""

    name: str
    keyword: str
    url: str


@dataclass(frozen=True, slots=True)
class WebsiteShortcut:
    keyword: str
    title: str
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.keyword, str) or not _KEYWORD.fullmatch(self.keyword):
            raise ValueError("Website keywords may contain letters, numbers, _ and -.")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Give each website a title.")
        website_origin(self.url)

    @property
    def searchable(self) -> bool:
        """Whether the URL is a template for search text."""
        return PLACEHOLDER in self.url

    def target(self, search_text: str = "") -> WebsiteTarget:
        """Search for `search_text`; without it, open the website's home page."""
        if self.searchable and search_text:
            url = self.url.replace(PLACEHOLDER, quote(search_text, safe=""))
            return WebsiteTarget(f"Search {self.title} for {search_text}", self.keyword, url)
        page = website_origin(self.url) if self.searchable else self.url
        return WebsiteTarget(self.title, self.keyword, page)


DEFAULT_WEBSITES = (
    WebsiteShortcut("g", "Google", "https://www.google.com/search?q=%s"),
    WebsiteShortcut("az", "Amazon", "https://www.amazon.de/s?k=%s"),
    WebsiteShortcut("yt", "YouTube", "https://www.youtube.com/results?search_query=%s"),
    WebsiteShortcut("gm", "Google Maps", "https://maps.google.com/?q=%s"),
    WebsiteShortcut("eb", "eBay", "https://www.ebay.com/sch/i.html?_nkw=%s"),
    WebsiteShortcut("ka", "Kleinanzeigen", "https://www.kleinanzeigen.de/s-%s/k0"),
    WebsiteShortcut("imdb", "IMDb", "https://www.imdb.com/de/find/?q=%s&"),
)


@dataclass(frozen=True, slots=True)
class WebsiteQuery:
    """Typed text that starts with a website keyword.

    The same text also searches the catalog: `files` is that query, or None when
    the text is no valid file query, for example because of an unclosed quote.
    """

    shortcut: WebsiteShortcut
    search_text: str
    files: SearchQuery | None

    def target(self) -> WebsiteTarget:
        return self.shortcut.target(self.search_text)


def website_query(text: str, shortcuts: tuple[WebsiteShortcut, ...]) -> WebsiteQuery | None:
    """The website query `text` asks for, if it starts with a configured keyword."""
    keyword, _, search_text = text.strip().partition(" ")
    shortcut = next((s for s in shortcuts if normalize(s.keyword) == normalize(keyword)), None)
    search_text = search_text.strip()
    if shortcut is None or (search_text and not shortcut.searchable):
        return None
    try:
        files = parse_query(text)
    except ValueError:
        files = None
    return WebsiteQuery(shortcut, search_text, files)
