"""Internet reachability hints shared by the launcher and its plugins."""

from dataclasses import dataclass
from enum import StrEnum


class InternetStatus(StrEnum):
    """A connectivity hint, never a guarantee that a plugin's host is reachable."""

    UNKNOWN = "unknown"
    OFFLINE = "offline"
    ONLINE = "online"


@dataclass(frozen=True, slots=True)
class InternetState:
    """The latest Windows and HTTPS observations; None means unknown."""

    windows_online: bool | None = None
    http_online: bool | None = None
    checked_at: float | None = None

    @property
    def status(self) -> InternetStatus:
        """Either success confirms connectivity; only two failures declare offline."""
        if self.windows_online is True or self.http_online is True:
            return InternetStatus.ONLINE
        if self.windows_online is False and self.http_online is False:
            return InternetStatus.OFFLINE
        return InternetStatus.UNKNOWN

    @property
    def ping_online(self) -> bool | None:
        """Compatibility alias for plugins written for the ICMP probe."""
        return self.http_online
