"""Connectivity hints and HTTP replies without relying on a live internet connection."""

import ctypes
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

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
        for (windows, http), status in expected.items():
            with self.subTest(windows=windows, http=http):
                self.assertEqual(InternetState(windows, http).status, status)

    def test_probe_keeps_the_individual_results_and_observation_time(self):
        with (
            patch.object(connectivity.os, "name", "nt"),
            patch.object(connectivity, "_windows_online", return_value=True),
            patch.object(connectivity, "_http_online", return_value=False),
            patch.object(connectivity.time, "time", return_value=42),
        ):
            self.assertEqual(connectivity.probe_internet(), InternetState(True, False, 42))

    def test_http_expects_204_and_closes_the_response(self):
        for status in (204, 200, 302):
            with self.subTest(status=status):
                response = MagicMock()
                response.__enter__.return_value.status = status
                with patch.object(connectivity, "urlopen", return_value=response) as request:
                    self.assertEqual(connectivity._http_online(), status == 204)
                    request.assert_called_once_with(
                        "https://www.gstatic.com/generate_204",
                        timeout=connectivity.HTTP_TIMEOUT_SECONDS,
                    )
                response.__exit__.assert_called_once()

    def test_http_failures_are_offline(self):
        errors = (
            URLError("No connection"),
            TimeoutError("Timed out"),
            HTTPError(connectivity.HTTP_PROBE_URL, 503, "Unavailable", None, None),
        )
        for error in errors:
            with (
                self.subTest(error=error),
                patch.object(connectivity, "urlopen", side_effect=error),
            ):
                self.assertFalse(connectivity._http_online())

    def test_legacy_ping_attribute_exposes_the_http_observation(self):
        for online in (True, False, None):
            self.assertIs(InternetState(http_online=online).ping_online, online)

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
