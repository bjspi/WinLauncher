"""Fetch bounded favicon images asynchronously, directly from the configured website."""

from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QBitmap, QIcon, QImageReader, QPainter, QPixmap, QRegion
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from pathscout.websites import website_origin

MAX_ICON_BYTES = 256 * 1024


class WebsiteIcons(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fallback = QIcon(str(Path(__file__).with_name("assets") / "globe.svg"))
        self.network = QNetworkAccessManager(self)
        self._cache: dict[str, QIcon] = {}
        self._pending: dict[str, QNetworkReply] = {}
        self._closed = False

    def icon_for(self, url: str) -> QIcon:
        try:
            origin = website_origin(url)
        except ValueError:
            return self.fallback
        if origin in self._cache:
            return self._cache[origin]
        if not self._closed and origin not in self._pending and len(self._pending) < 8:
            # shortcut: use /favicon.ico; add HTML icon discovery if sites require it.
            request = QNetworkRequest(QUrl(origin + "favicon.ico"))
            request.setTransferTimeout(10000)
            request.setAttribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute,
                QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
            )
            reply = self.network.get(request)
            self._pending[origin] = reply
            reply.setReadBufferSize(MAX_ICON_BYTES + 1)
            reply.readyRead.connect(
                lambda: reply.abort() if reply.bytesAvailable() > MAX_ICON_BYTES else None
            )
            reply.downloadProgress.connect(
                lambda received, total: (
                    reply.abort() if max(received, total) > MAX_ICON_BYTES else None
                )
            )
            reply.finished.connect(lambda: self._finished(origin, reply))
        return self.fallback

    def _finished(self, origin: str, reply: QNetworkReply) -> None:
        self._pending.pop(origin, None)
        icon = self.fallback
        error = reply.error()
        if not self._closed and error == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            if len(data) <= MAX_ICON_BYTES:
                buffer = QBuffer()
                buffer.setData(data)
                buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                reader = QImageReader(buffer)
                size = reader.size()
                if size.isValid() and max(size.width(), size.height()) <= 1024:
                    image = reader.read()
                    if not image.isNull():
                        bounds = QRegion(QBitmap.fromImage(image.createAlphaMask())).boundingRect()
                        if not bounds.isEmpty():
                            image = image.copy(bounds)
                        image = image.scaled(
                            64,
                            64,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        canvas = QPixmap(64, 64)
                        canvas.fill(Qt.GlobalColor.transparent)
                        painter = QPainter(canvas)
                        painter.drawImage(
                            (64 - image.width()) // 2, (64 - image.height()) // 2, image
                        )
                        painter.end()
                        icon = QIcon(canvas)
        reply.deleteLater()
        if not self._closed:
            self._cache[origin] = icon
            if error in (
                QNetworkReply.NetworkError.TimeoutError,
                QNetworkReply.NetworkError.TemporaryNetworkFailureError,
                QNetworkReply.NetworkError.RemoteHostClosedError,
                QNetworkReply.NetworkError.HostNotFoundError,
                QNetworkReply.NetworkError.ConnectionRefusedError,
            ):
                QTimer.singleShot(15000, self, lambda: self._retry(origin))
            self.changed.emit()

    def _retry(self, origin: str) -> None:
        if not self._closed:
            self._cache.pop(origin, None)
            self.changed.emit()  # Only visible consumers request the icon again.

    def close(self) -> None:
        self._closed = True
        for reply in tuple(self._pending.values()):
            reply.abort()
