"""Menus: a click right after a menu opens does not trigger the entry under the pointer."""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from winlauncher.ui.menus import CLICK_GUARD_S, Menu


class MenuClickGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_a_stray_click_right_after_opening_does_not_quit(self) -> None:
        menu = Menu()
        self.addCleanup(menu.deleteLater)
        quits: list[bool] = []
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(lambda: quits.append(True))
        menu.popup(QPoint(200, 200))
        QTest.qWaitForWindowExposed(menu)
        center = menu.actionGeometry(quit_action).center()

        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=center)
        self.assertFalse(quits)

        QTest.qWait(int(CLICK_GUARD_S * 1000) + 100)
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=center)
        self.assertTrue(quits)


if __name__ == "__main__":
    unittest.main()
