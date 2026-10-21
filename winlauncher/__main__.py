"""Application entry point and single-instance ownership.

One process per data directory owns the lock file and a local server. A second
start asks that owner to show its launcher and exits.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import queue
import sys
import threading
import time
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QEventLoop, QLockFile, QProcess, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket, QSslSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QSystemTrayIcon

from winlauncher import __version__
from winlauncher.catalog.model import path_key
from winlauncher.install import (
    REINSTALL_TIMEOUT_MS,
    reinstall_arguments,
    reinstall_command,
    take_pending_reinstall,
)
from winlauncher.platform.git import checkout_root
from winlauncher.ui.controller import LauncherController
from winlauncher.ui.theme import STYLESHEET, app_icon

SHOW_REQUEST = b"show"
PING_REQUEST = b"ping"  # A second background start only confirms the owner.
HANDOVER_TIMEOUT_SECONDS = 8
REPLACE_WAIT_MS = 15_000  # A restarting instance releases the lock while quitting.
CONNECT_TIMEOUT_MS = 200
WRITE_TIMEOUT_MS = 500
RETRY_PAUSE_SECONDS = 0.05
LOG_BYTES = 5_000_000
LOG_BACKUPS = 5
WINDOWS_TLS_BACKEND = "schannel"


def _default_data_directory() -> Path:
    return Path.home() / ".WinLauncher"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WinLauncher — local Windows search")
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
        help="Start in the tray (the default); a second start then leaves WinLauncher alone",
    )
    parser.add_argument(
        "--no-hooks", action="store_true", help="Disable global shortcuts and Explorer typing"
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Wait for the running instance to exit and take over (used by Restart)",
    )
    parser.add_argument("--version", action="version", version=f"WinLauncher {__version__}")
    return parser.parse_args()


def _create_application() -> QApplication:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("WinLauncher")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)  # The tray keeps WinLauncher running.
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    _use_windows_tls()
    return app


def _use_windows_tls() -> None:
    """Fetch website icons over Windows' own TLS (Schannel) rather than OpenSSL.

    Qt loads whichever OpenSSL DLL comes first on PATH; another program's copy,
    such as Git's, lacks functions Qt needs and crashes the process. Listing the
    available backends would already load it, so this only asks for Schannel.
    """
    if not QSslSocket.setActiveBackend(WINDOWS_TLS_BACKEND):
        logging.warning("Windows TLS is unavailable; using %s", QSslSocket.activeBackend())


def _configure_logging(data_dir: Path) -> QueueListener:
    data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        data_dir / "winlauncher.log", maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    messages = queue.SimpleQueue()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(process)d:%(threadName)s] %(name)s: %(message)s"
        )
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[QueueHandler(messages)],
        # The queue carries the bare message; the file handler formats it once.
        format="%(message)s",
        force=True,
    )
    listener = QueueListener(messages, handler)
    listener.start()
    return listener


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


def _finish_update(root: Path) -> None:
    """Reinstall the updated checkout before anything loads the old Rust engine."""
    progress = QProgressDialog("Finishing the WinLauncher update…", None, 0, 0)
    progress.setWindowTitle("WinLauncher")
    progress.setMinimumDuration(0)
    progress.show()
    process = QProcess()
    process.setWorkingDirectory(str(root))
    process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
    waiting = QEventLoop()
    process.finished.connect(waiting.quit)
    process.errorOccurred.connect(waiting.quit)  # A failed start emits no `finished`.
    QTimer.singleShot(REINSTALL_TIMEOUT_MS, process.kill)
    process.start(sys.executable, reinstall_arguments())
    waiting.exec()
    progress.close()
    output = bytes(process.readAll().data()).decode("utf-8", errors="replace").strip()
    succeeded = process.exitStatus() == QProcess.ExitStatus.NormalExit and process.exitCode() == 0
    logging.info("Update reinstall finished: succeeded=%s output=%s", succeeded, output)
    if not succeeded:
        QMessageBox.warning(
            None,
            "WinLauncher update incomplete",
            "The new version runs, but its packages could not be reinstalled, so the "
            f"Rust search engine may be unavailable.\n\n{output}\n\n"
            f"Run this once to finish:\n{reinstall_command(root)}",
        )


def _instance_name(data_dir: Path) -> str:
    return "winlauncher-" + hashlib.sha256(path_key(data_dir).encode()).hexdigest()[:20]


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
            logging.info("Show requested by a second process")
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
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        QMessageBox.critical(None, "WinLauncher cannot start", str(exc))
        return 1
    instance = _instance_name(data_dir)
    ownership = QLockFile(str(data_dir / "instance.lock"))
    ownership.setStaleLockTime(0)  # Only a dead owner process releases the lock.
    if not ownership.tryLock(REPLACE_WAIT_MS if args.replace else 0):
        if args.replace:
            QMessageBox.critical(
                None, "WinLauncher cannot restart", "The previous instance did not exit in time."
            )
            return 1
        request = PING_REQUEST if args.background else SHOW_REQUEST
        if _hand_over_to_owner(instance, request):
            return 0
        QMessageBox.critical(
            None,
            "WinLauncher is already starting",
            f"The primary instance did not respond within {HANDOVER_TIMEOUT_SECONDS} seconds.",
        )
        return 1

    server = QLocalServer()
    listener = None
    try:
        # Only the elected owner writes logs, so a second start cannot race rotation.
        listener = _configure_logging(data_dir)
        _log_uncaught_exceptions()
        logging.info(
            "WinLauncher %s starting: Python=%s data_dir=%s hooks=%s",
            __version__,
            sys.version.split()[0],
            data_dir,
            not args.no_hooks,
        )
        if take_pending_reinstall(data_dir) and (root := checkout_root()) is not None:
            _finish_update(root)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(instance):
            raise OSError(server.errorString())
        controller = LauncherController(data_dir, hooks=not args.no_hooks)
        _serve_show_requests(server, controller)
        app.aboutToQuit.connect(controller.close)
        # WinLauncher starts quietly in the tray; without a tray it would be unreachable.
        if args.show or not QSystemTrayIcon.isSystemTrayAvailable():
            controller.window.present()
        return app.exec()
    except Exception as exc:
        logging.exception("Startup failed")
        settings_path = data_dir / "settings.json"
        QMessageBox.critical(
            None, "WinLauncher cannot start", f"{exc}\n\nSettings: {settings_path}"
        )
        return 1
    finally:
        server.close()
        if listener is not None:
            logging.info("WinLauncher stopping")
            listener.stop()
            for handler in listener.handlers:
                handler.close()
        ownership.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
