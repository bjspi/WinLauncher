"""Connectivity hints and ICMP replies without relying on a live internet connection."""

import ctypes
import unittest
from unittest.mock import Mock, patch

from winlauncher.connectivity import InternetState, InternetStatus
from winlauncher.platform import connectivity


class ConnectivityTest(unittest.TestCase):
    def test_only_two_failures_mean_offline_and_either_success_means_online(self):
        expected = {
            (None, None): InternetStatus.UNKNOWN,
            (False, None): InternetStatus.UNKNOWN,
            (None, False): InternetStatus.UNKNOWN,
            (False, False): InternetStatus.OFFLINE,
            (True, False): InternetStatus.ONLINE,
            (False, True): InternetStatus.ONLINE,
            (True, None): InternetStatus.ONLINE,
            (None, True): InternetStatus.ONLINE,
            (True, True): InternetStatus.ONLINE,
        }
        for (windows, ping), status in expected.items():
            with self.subTest(windows=windows, ping=ping):
                self.assertEqual(InternetState(windows, ping).status, status)

    def test_probe_keeps_the_individual_results_and_observation_time(self):
        with (
            patch.object(connectivity.os, "name", "nt"),
            patch.object(connectivity, "_windows_online", return_value=True),
            patch.object(connectivity, "_ping_online", return_value=False),
            patch.object(connectivity.time, "time", return_value=42),
        ):
            self.assertEqual(connectivity.probe_internet(), InternetState(True, False, 42))

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows ICMP API")
    def test_ping_checks_reply_status_timeout_and_closes_its_handle(self):
        library = Mock()
        library.IcmpCreateFile.return_value = 123

        def reply(*args):
            connectivity._EchoReply.from_buffer(args[5]).status = reply_status
            self.assertEqual(args[-1], connectivity.PING_TIMEOUT_MS)
            self.assertEqual(args[1], int.from_bytes(bytes([1, 1, 1, 1]), "little"))
            return 1

        library.IcmpSendEcho.side_effect = reply
        with patch.object(ctypes, "WinDLL", return_value=library):
            for reply_status in (0, 11003):  # Echo success versus destination unreachable.
                with self.subTest(reply_status=reply_status):
                    self.assertEqual(connectivity._ping_online(), reply_status == 0)
                    library.IcmpCloseHandle.assert_called_with(123)
            library.IcmpSendEcho.side_effect = None
            library.IcmpSendEcho.return_value = 0
            self.assertFalse(connectivity._ping_online())
            library.IcmpCloseHandle.assert_called_with(123)
            library.IcmpCloseHandle.reset_mock()
            library.IcmpCreateFile.return_value = ctypes.c_void_p(-1).value
            self.assertIsNone(connectivity._ping_online())
            library.IcmpCloseHandle.assert_not_called()
        with patch.object(ctypes, "WinDLL", side_effect=OSError("Unavailable")):
            self.assertIsNone(connectivity._ping_online())

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows Network List Manager")
    def test_windows_probe_cleans_up_com_even_if_network_list_manager_fails(self):
        import pythoncom
        import win32com.client

        with (
            patch.object(pythoncom, "CoInitialize") as initialize,
            patch.object(pythoncom, "CoUninitialize") as uninitialize,
            patch.object(win32com.client, "Dispatch") as dispatch,
        ):
            dispatch.return_value.IsConnectedToInternet = True
            self.assertTrue(connectivity._windows_online())
            dispatch.side_effect = OSError("Network List Manager unavailable")
            self.assertIsNone(connectivity._windows_online())
            self.assertEqual(initialize.call_count, 2)
            self.assertEqual(uninitialize.call_count, 2)
