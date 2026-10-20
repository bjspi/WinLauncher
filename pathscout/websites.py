"""Configured website keywords and URL templates; no browser or GUI access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote, urlsplit, urlunsplit

from pathscout.catalog.model import normalize


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
            and "%s" not in parts.netloc
        )
        _ = parts.port  # Validate a supplied port, too.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Use an http:// or https:// website URL without login credentials.")
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


@dataclass(frozen=True, slots=True)
class WebsiteShortcut:
    keyword: str
    title: str
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.keyword, str) or not re.fullmatch(r"[\w-]+", self.keyword):
            raise ValueError("Website keywords may contain letters, numbers, _ and -.")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("Give each website a title.")
        website_origin(self.url)

    def target(self, search_text: str = "") -> WebsiteTarget:
        if "%s" in self.url:
            url = (
                self.url.replace("%s", quote(search_text, safe=""))
                if search_text
                else website_origin(self.url)
            )
        else:
            url = self.url
        name = (
            f"Search {self.title} for {search_text}"
            if search_text and "%s" in self.url
            else self.title
        )
        return WebsiteTarget(name, self.keyword, url)


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
class WebsiteTarget:
    name: str
    keyword: str
    url: str
    pinned: ClassVar[bool] = False


@dataclass(frozen=True, slots=True)
class WebsiteQuery:
    shortcut: WebsiteShortcut
    search_text: str

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(normalize(self.search_text).split())

    @property
    def history_key(self) -> str:
        return ""  # Website searches are not stored in the file catalog's history.


def website_query(text: str, shortcuts: tuple[WebsiteShortcut, ...]) -> WebsiteQuery | None:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    keyword = normalize(parts[0])
    for shortcut in shortcuts:
        if normalize(shortcut.keyword) == keyword:
            search_text = parts[1] if len(parts) == 2 else ""
            if search_text and "%s" not in shortcut.url:
                return None
            return WebsiteQuery(shortcut, search_text)
    return None
