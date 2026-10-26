"""Observe Windows' internet status and ping Cloudflare's public DNS server."""

from __future__ import annotations

import ctypes
import os
import socket
import time
from ctypes import wintypes

from winlauncher.connectivity import InternetState

PING_HOST = "1.1.1.1"
PING_TIMEOUT_MS = 1000
NETWORK_LIST_MANAGER = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
IP_SUCCESS = 0
ICMP_ERROR_BYTES = 8  # IcmpSendEcho requires room for an ICMP error after the echo reply.


class _IpOptions(ctypes.Structure):
    _fields_ = [
        ("ttl", wintypes.BYTE),
        ("tos", wintypes.BYTE),
        ("flags", wintypes.BYTE),
        ("options_size", wintypes.BYTE),
        ("options_data", ctypes.c_void_p),
    ]


class _EchoReply(ctypes.Structure):
    _fields_ = [
        ("address", wintypes.ULONG),
        ("status", wintypes.ULONG),
        ("round_trip_time", wintypes.ULONG),
        ("data_size", wintypes.USHORT),
        ("reserved", wintypes.USHORT),
        ("data", ctypes.c_void_p),
        ("options", _IpOptions),
    ]


def probe_internet() -> InternetState:
    """Run on a network worker: no DNS lookup, console, or plugin worker is needed."""
    if os.name != "nt":
        return InternetState(checked_at=time.time())
    windows_online = _windows_online()
    ping_online = _ping_online()
    return InternetState(windows_online, ping_online, time.time())


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


def _ping_online() -> bool | None:
    """Use Windows ICMP directly: ping.exe can report success for an unreachable reply."""
    try:
        icmp = ctypes.WinDLL("iphlpapi", use_last_error=True)
        icmp.IcmpCreateFile.argtypes = []
        icmp.IcmpCreateFile.restype = wintypes.HANDLE
        icmp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        icmp.IcmpCloseHandle.restype = wintypes.BOOL
        icmp.IcmpSendEcho.argtypes = [
            wintypes.HANDLE,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.WORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        icmp.IcmpSendEcho.restype = wintypes.DWORD
        handle = icmp.IcmpCreateFile()
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            payload = b"WinLauncher"
            reply = ctypes.create_string_buffer(
                ctypes.sizeof(_EchoReply) + len(payload) + ICMP_ERROR_BYTES
            )
            # IPAddr stores the IPv4 octets in network order in memory.
            address = int.from_bytes(socket.inet_aton(PING_HOST), "little")
            received = icmp.IcmpSendEcho(
                handle, address, payload, len(payload), None, reply, len(reply), PING_TIMEOUT_MS
            )
            return received > 0 and _EchoReply.from_buffer(reply).status == IP_SUCCESS
        finally:
            icmp.IcmpCloseHandle(handle)
    except OSError:
        return None
