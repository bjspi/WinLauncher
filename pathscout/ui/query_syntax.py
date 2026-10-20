"""Color filters and exclusions inside the search field while the query is typed."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject
from PySide6.QtGui import QColor, QInputMethodEvent, QTextCharFormat
from PySide6.QtWidgets import QLineEdit

from pathscout.search.query_parser import QuerySpan, SpanRole, query_spans
from pathscout.ui.theme import Palette


def _format(foreground: str, background: str) -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(foreground))
    text_format.setBackground(QColor(background))
    return text_format


def _invalid_format() -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(Palette.INVALID))
    text_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
    text_format.setUnderlineColor(QColor(Palette.INVALID))
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
        self.formats = {
            SpanRole.FILTER: _format(Palette.FILTER, Palette.FILTER_BACKGROUND),
            SpanRole.CONTENT: _format(Palette.CONTENT, Palette.CONTENT_BACKGROUND),
            SpanRole.EXCLUSION: _format(Palette.EXCLUSION, Palette.EXCLUSION_BACKGROUND),
        }
        self.invalid = _invalid_format()
        field.textChanged.connect(self.refresh)
        # Leaving a filter may reveal that its value is invalid.
        field.cursorPositionChanged.connect(self.refresh)

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
