# Third-party notices

PathScout's original source is provided under the [MIT license](LICENSE).
Dependencies retain their own licenses. The repository does not bundle dependency
binaries; normal installation obtains them as separate Python packages.

| Component | Purpose | License / upstream |
| --- | --- | --- |
| PySide6, Shiboken6, Qt | Desktop widgets and Qt runtime | Installed PySide6 6.11.1 metadata declares `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html) |
| pywin32 | Windows COM, registry and directory APIs | Python Software Foundation license; [upstream license](https://github.com/mhammond/pywin32/blob/main/License.txt) |
| Python | Interpreter, sqlite3 and other standard-library modules | [Python license and notices](https://docs.python.org/3/license.html) |
| SQLite | Local catalog engine through Python's sqlite3 | [Public domain](https://www.sqlite.org/copyright.html) |

If distributing an executable bundle, include the actual dependencies' license
texts and notices, preserve dynamic library replacement/relinking rights required
by the chosen license, and review the licenses of the exact Qt modules included.
An MIT license for this project's code does not relicense Qt or other dependencies.
No frozen executable is currently distributed by this project.
