"""Editors for plugin options, alike on a plugin's settings card and in its forms.

A `PluginOption` names its type by its default: a switch for `bool`, a number
field for `int`, a text box for multiline text and a line for other text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QWidget

from winlauncher.plugins.api import OptionValue, PluginOption
from winlauncher.ui.widgets import NumberField, ToggleSwitch, sized_number

TEXT_OPTION_WIDTH = 220
TEXT_OPTION_HEIGHT = 96  # About five lines for a multiline option.
UNBOUNDED = (-(2**31), 2**31 - 1)  # What a number field holds without the option's bounds.


@dataclass(frozen=True, slots=True)
class OptionEditor:
    """The control for one option, and how to read the value the user left in it."""

    control: QWidget
    read: Callable[[], OptionValue]


def option_editor(option: PluginOption, value: OptionValue) -> OptionEditor:
    """The control `option`'s type calls for, showing `value`."""
    if isinstance(value, bool):
        switch = ToggleSwitch(option.label)
        switch.setChecked(value)
        return OptionEditor(switch, switch.isChecked)
    if isinstance(value, int):
        field = sized_number(NumberField(option.bounds or UNBOUNDED, value))
        field.setAccessibleName(option.label)
        return OptionEditor(field, field.value)
    if option.multiline:
        box = QPlainTextEdit(str(value))
        box.setFixedWidth(TEXT_OPTION_WIDTH)
        box.setFixedHeight(TEXT_OPTION_HEIGHT)
        box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        box.setAccessibleName(option.label)
        return OptionEditor(box, box.toPlainText)
    line = QLineEdit(str(value))
    line.setFixedWidth(TEXT_OPTION_WIDTH)
    line.setAccessibleName(option.label)
    return OptionEditor(line, line.text)
