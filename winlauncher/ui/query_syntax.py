"""Color filters and exclusions inside the search field while the query is typed."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtGui import QColor, QInputMethodEvent, QTextCharFormat
from PySide6.QtWidgets import QLineEdit

from winlauncher.search.query_parser import QuerySpan, SpanRole, query_spans
from winlauncher.ui.theme import colors, theme


def _format(foreground: str, background: str) -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(foreground))
    text_format.setBackground(QColor(background))
    return text_format


def _invalid_format() -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(colors().invalid))
    text_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
    text_format.setUnderlineColor(QColor(colors().invalid))
    return text_format


class QuerySyntax(QObject):
    """Formats `field` as `parse_query` reads it: filters, content searches, exclusions.

    QLineEdit has no rich text, but it renders the text formats of an input-method
    event. An event without preedit or commit text changes only those formats, so
    the text, cursor, selection and undo history stay untouched.
    """

    def __init__(self, field: QLineEdit):
        super().__init__(field)
        self.field = field
        self._use_theme_colors()
        theme().changed.connect(self._theme_changed)
        field.textChanged.connect(self.refresh)
        # Leaving a filter may reveal that its value is invalid.
        field.cursorPositionChanged.connect(self.refresh)

    def _use_theme_colors(self) -> None:
        self.formats = {
            SpanRole.FILTER: _format(colors().filter, colors().filter_background),
            SpanRole.CONTENT: _format(colors().content, colors().content_background),
            SpanRole.EXCLUSION: _format(colors().exclusion, colors().exclusion_background),
        }
        self.invalid = _invalid_format()

    def _theme_changed(self) -> None:
        self._use_theme_colors()
        self.refresh()

    def refresh(self) -> None:
        """Re-format the current text; call it after changing text with signals blocked."""
        cursor = self.field.cursorPosition()
        attributes = [
            QInputMethodEvent.Attribute(
                QInputMethodEvent.AttributeType.TextFormat,
                span.start - cursor,  # Positions are relative to the cursor.
                span.end - span.start,
                self._format(span, cursor),
            )
            for span in query_spans(self.field.text())
        ]
        QCoreApplication.sendEvent(self.field, QInputMethodEvent("", attributes))

    def _format(self, span: QuerySpan, cursor: int) -> QTextCharFormat:
        """The filter being typed at the cursor is never marked invalid; it may be unfinished."""
        being_typed = span.start <= cursor <= span.end
        return self.formats[span.role] if span.valid or being_typed else self.invalid
