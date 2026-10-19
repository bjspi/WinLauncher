"""Application entry point and single-instance ownership.

One process per data directory owns the lock file and a local server. A second
start asks that owner to show its launcher and exits.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from pathscout import __version__
from pathscout.catalog.model import path_key
from pathscout.ui.controller import LauncherController
from pathscout.ui.theme import STYLESHEET, app_icon

SHOW_REQUEST = b"show"
PING_REQUEST = b"ping"  # A second background start only confirms the owner.
HANDOVER_TIMEOUT_SECONDS = 8
REPLACE_WAIT_MS = 15_000  # A restarting instance releases the lock while quitting.
CONNECT_TIMEOUT_MS = 200
WRITE_TIMEOUT_MS = 500
RETRY_PAUSE_SECONDS = 0.05
LOG_BYTES = 1_000_000
LOG_BACKUPS = 2


def _default_data_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))
    return Path(base) / "PathScout"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PathScout — local Windows search")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_default_data_directory(),
        help="Directory for settings, catalog and logs",
    )
    parser.add_argument(
        "--show", action="store_true", help="Open the search bar right after starting"
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start in the tray (the default); a second start then leaves PathScout alone",
    )
    parser.add_argument(
        "--no-hooks", action="store_true", help="Disable global shortcuts and Explorer typing"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Wait for the running instance to exit and take over (used by Restart)",
    )
    parser.add_argument("--version", action="version", version=f"PathScout {__version__}")
    return parser.parse_args()


def _create_application() -> QApplication:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PathScout")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)  # The tray keeps PathScout running.
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    return app


def _configure_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        data_dir / "pathscout.log", maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_uncaught_exceptions() -> None:
    """Under pythonw there is no console; unhandled errors must reach the log file."""

    def log_exception(kind, value, traceback) -> None:
        logging.critical("Unhandled exception", exc_info=(kind, value, traceback))

    def log_thread_exception(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread else "unknown thread"
        error = (args.exc_type, args.exc_value, args.exc_traceback)
        logging.critical("Unhandled exception in %s", name, exc_info=error)

    sys.excepthook = log_exception
    threading.excepthook = log_thread_exception


def _instance_name(data_dir: Path) -> str:
    return "pathscout-" + hashlib.sha256(path_key(data_dir).encode()).hexdigest()[:20]


def _hand_over_to_owner(instance: str, request: bytes) -> bool:
    """Retry while a starting owner has the lock but no server yet."""
    deadline = time.monotonic() + HANDOVER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _send_request(instance, request):
            return True
        time.sleep(RETRY_PAUSE_SECONDS)
    return False


def _send_request(instance: str, request: bytes) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(instance)
    if socket.waitForConnected(CONNECT_TIMEOUT_MS):
        socket.write(request)
        if socket.waitForBytesWritten(WRITE_TIMEOUT_MS):
            socket.disconnectFromServer()
            return True
    socket.abort()
    return False


def _serve_show_requests(server: QLocalServer, controller: LauncherController) -> None:
    def receive(client: QLocalSocket) -> None:
        if bytes(client.readAll()) == SHOW_REQUEST:
            controller.window.present()
        client.disconnectFromServer()

    def accept_clients() -> None:
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            client.readyRead.connect(lambda client=client: receive(client))
            client.disconnected.connect(client.deleteLater)
            if client.bytesAvailable():
                receive(client)

    server.newConnection.connect(accept_clients)


def main() -> int:
    args = _parse_arguments()
    app = _create_application()
    data_dir = args.data_dir.expanduser().resolve()
    try:
        _configure_logging(data_dir)
    except OSError as exc:
        QMessageBox.critical(None, "PathScout cannot start", str(exc))
        return 1
    _log_uncaught_exceptions()

    instance = _instance_name(data_dir)
    ownership = QLockFile(str(data_dir / "instance.lock"))
    ownership.setStaleLockTime(0)  # Only a dead owner process releases the lock.
    if not ownership.tryLock(REPLACE_WAIT_MS if args.replace else 0):
        if args.replace:
            QMessageBox.critical(
                None, "PathScout cannot restart", "The previous instance did not exit in time."
            )
            return 1
        request = PING_REQUEST if args.background else SHOW_REQUEST
        if _hand_over_to_owner(instance, request):
            return 0
        QMessageBox.critical(
            None,
            "PathScout is already starting",
            f"The primary instance did not respond within {HANDOVER_TIMEOUT_SECONDS} seconds.",
        )
        return 1

    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(instance):
        QMessageBox.critical(None, "PathScout is already starting", server.errorString())
        return 1
    try:
        controller = LauncherController(data_dir, hooks=not args.no_hooks)
    except Exception as exc:
        logging.exception("Startup failed")
        settings_path = data_dir / "settings.json"
        QMessageBox.critical(None, "PathScout cannot start", f"{exc}\n\nSettings: {settings_path}")
        return 1

    _serve_show_requests(server, controller)
    app.aboutToQuit.connect(controller.close)
    # PathScout starts quietly in the tray; without a tray it would be unreachable.
    if args.show or not QSystemTrayIcon.isSystemTrayAvailable():
        controller.window.present()
    try:
        return app.exec()
    finally:
        server.close()
        ownership.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
