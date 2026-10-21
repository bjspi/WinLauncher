"""Fetch bounded favicon images asynchronously, directly from the configured website."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBitmap, QIcon, QImage, QImageReader, QPainter, QPixmap, QRegion
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from winlauncher.websites import website_origin

MAX_ICON_BYTES = 256 * 1024
MAX_ICON_DIMENSION = 1024  # Larger images are refused before decoding.
ICON_SIZE = 64  # Square pixmap that holds the scaled favicon.
MAX_PENDING_REQUESTS = 8
REQUEST_TIMEOUT_MS = 10_000
RETRY_DELAY_MS = 15_000
# Failures that may pass; other errors keep the fallback until WinLauncher restarts.
TRANSIENT_ERRORS = frozenset(
    {
        QNetworkReply.NetworkError.TimeoutError,
        QNetworkReply.NetworkError.TemporaryNetworkFailureError,
        QNetworkReply.NetworkError.RemoteHostClosedError,
        QNetworkReply.NetworkError.HostNotFoundError,
        QNetworkReply.NetworkError.ConnectionRefusedError,
    }
)


class WebsiteIcons(QObject):
    """Favicons per website origin; `changed` fires when one arrives or may be retried."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.fallback = QIcon(str(Path(__file__).with_name("assets") / "globe.svg"))
        self.network = QNetworkAccessManager(self)
        self._cache: dict[str, QIcon] = {}
        self._pending: dict[str, QNetworkReply] = {}
        self._closed = False

    def icon_for(self, url: str) -> QIcon:
        """The cached favicon of `url`'s website; the globe while it loads or if it fails."""
        try:
            origin = website_origin(url)
        except ValueError:
            return self.fallback
        if origin in self._cache:
            return self._cache[origin]
        if (
            not self._closed
            and origin not in self._pending
            and len(self._pending) < MAX_PENDING_REQUESTS
        ):
            self._request(origin)
        return self.fallback

    def close(self) -> None:
        self._closed = True
        for reply in tuple(self._pending.values()):
            reply.abort()

    def _request(self, origin: str) -> None:
        # Only the conventional /favicon.ico; icons declared in a page's HTML are not looked up.
        request = QNetworkRequest(QUrl(origin + "favicon.ico"))
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        reply = self.network.get(request)
        self._pending[origin] = reply
        # Buffer at most one byte over the limit; the progress check then aborts.
        reply.setReadBufferSize(MAX_ICON_BYTES + 1)
        reply.downloadProgress.connect(
            lambda received, total: reply.abort() if max(received, total) > MAX_ICON_BYTES else None
        )
        reply.finished.connect(lambda: self._finished(origin, reply))

    def _finished(self, origin: str, reply: QNetworkReply) -> None:
        self._pending.pop(origin, None)
        reply.deleteLater()
        if self._closed:
            return
        error = reply.error()
        icon = (
            _decode_icon(reply.readAll()) if error == QNetworkReply.NetworkError.NoError else None
        )
        self._cache[origin] = icon or self.fallback
        if error in TRANSIENT_ERRORS:
            QTimer.singleShot(RETRY_DELAY_MS, self, lambda: self._retry(origin))
        self.changed.emit()

    def _retry(self, origin: str) -> None:
        if not self._closed:
            self._cache.pop(origin, None)
            self.changed.emit()  # Only visible consumers request the icon again.


def _decode_icon(data: QByteArray) -> QIcon | None:
    """A centered square icon from favicon bytes, or None if they are no usable image."""
    if len(data) > MAX_ICON_BYTES:
        return None
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()
    if not size.isValid() or max(size.width(), size.height()) > MAX_ICON_DIMENSION:
        return None
    image = reader.read()
    if image.isNull():
        return None
    image = _crop_transparent_border(image).scaled(
        ICON_SIZE,
        ICON_SIZE,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QPixmap(ICON_SIZE, ICON_SIZE)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.drawImage((ICON_SIZE - image.width()) // 2, (ICON_SIZE - image.height()) // 2, image)
    painter.end()
    return QIcon(canvas)


def _crop_transparent_border(image: QImage) -> QImage:
    """Many favicons pad their artwork with transparency, which would make it look small.

    The bounding box of the alpha mask is the visible artwork; fully transparent
    images stay as they are.
    """
    visible = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
    return image if visible.isEmpty() else image.copy(visible)
