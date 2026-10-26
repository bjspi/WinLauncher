"""Observe Windows' internet status and Google's HTTPS connectivity endpoint."""

from __future__ import annotations

import os
import time
from http import HTTPStatus
from urllib.request import urlopen

from winlauncher.connectivity import InternetState

HTTP_PROBE_URL = "https://www.gstatic.com/generate_204"
HTTP_TIMEOUT_SECONDS = 2
NETWORK_LIST_MANAGER = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"


def probe_internet() -> InternetState:
    """Run on the dedicated network worker, without blocking plugins or the GUI."""
    if os.name != "nt":
        return InternetState(checked_at=time.time())
    windows_online = _windows_online()
    http_online = _http_online()
    return InternetState(windows_online, http_online, time.time())


def _windows_online() -> bool | None:
    """Ask Network List Manager on this worker's own COM apartment."""
    import pythoncom
    import win32com.client

    try:
        pythoncom.CoInitialize()
    except pythoncom.com_error:
        return None
    try:
        return bool(win32com.client.Dispatch(NETWORK_LIST_MANAGER).IsConnectedToInternet)
    except (pythoncom.com_error, OSError, AttributeError):
        return None
    finally:
        pythoncom.CoUninitialize()


def _http_online() -> bool:
    """Expect HTTP 204; a login portal's successful HTML response is not connectivity."""
    try:
        with urlopen(HTTP_PROBE_URL, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status == HTTPStatus.NO_CONTENT
    except OSError:
        return False
