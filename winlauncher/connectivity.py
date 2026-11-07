"""Internet reachability hints shared by the launcher and its plugins."""

import os
from dataclasses import dataclass
from enum import StrEnum

# Set to 1, WinLauncher contacts nothing on its own: no internet check, update check,
# or plugin source check. Test runs set it for the instances they
# start, which a firewall would otherwise ask about, from a new temporary path each
# time. What the user starts, such as Update or Install, still goes online.
OFFLINE_VARIABLE = "WINLAUNCHER_OFFLINE"


def automatic_network_off() -> bool:
    return os.environ.get(OFFLINE_VARIABLE, "") not in ("", "0")


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
