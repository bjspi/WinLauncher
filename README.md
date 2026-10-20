<div align="center">

<img src="docs/images/icon.png" width="96" alt="PathScout icon">

# PathScout

**Double-tap <kbd>Ctrl</kbd>. Type a few letters. Open anything.**

A fast, private launcher for Windows that finds your apps, files and folders —<br>
or searches the folder you are in when you simply start typing in Explorer.

[![Windows 10 | 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?logo=windows&logoColor=white)](#-requirements)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Qt for Python](https://img.shields.io/badge/UI-PySide6%20%2F%20Qt%206-41CD52?logo=qt&logoColor=white)](https://doc.qt.io/qtforpython-6/)
[![SQLite FTS5](https://img.shields.io/badge/Index-SQLite%20FTS5-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/fts5.html)
[![100% local](https://img.shields.io/badge/Privacy-100%25%20local-8A2BE2?logo=shield&logoColor=white)](#-privacy)
[![Ruff](https://img.shields.io/badge/code%20style-Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![License: MIT](https://img.shields.io/badge/License-MIT-F7DF1E)](LICENSE)

<img src="docs/images/launcher.png" width="620" alt="The PathScout search bar with results">

</div>

---

## ✨ Highlights

| | |
| --- | --- |
| ⚡ **Instant launcher** | Double-tap <kbd>Ctrl</kbd> or press <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> anywhere. A compact bar opens; results appear as you type. |
| 📂 **Type-to-search in Explorer** | Start typing in an Explorer file list and a slim bar docks to that window, searching the folder you are in — its own entries first. |
| 🧠 **Smart matching** | Exact, prefix, word starts, camelCase initials (`vsc` → *Visual Studio Code*) and fuzzy subsequences, with matched letters highlighted. |
| 📈 **Learns what you open** | Launches per query, favorites and recency lift the results you actually use. |
| 🧮 **Built-in calculator** | Type `200 + 10%`, `sqrt(2)` or `hex(255)` and the result appears above the search results; <kbd>Enter</kbd> copies it. |
| 🎯 **Rich query language** | `type:`, `ext:`, `in:`, `path:`, `name:`, dates, sorting, `!exclusions` and more. |
| 🚫 **Clean results by default** | Skips `.git*`, caches (`__pycache__`, `.ruff_cache`, `.mypy_cache`, …), virtual environments under any name, `node_modules`, `build`, `dist`, Gradle folders and folders marked with `.nobackup`. |
| 🔄 **Always current** | Watches your folders live; file changes refresh exactly that file, folder changes only their subtree. |
| ⌨️ **Keyboard first** | <kbd>Enter</kbd> opens, <kbd>Ctrl</kbd>+<kbd>1</kbd>…<kbd>9</kbd> opens a numbered result, <kbd>Ctrl</kbd>+<kbd>Enter</kbd> shows it in its folder. |
| 🔒 **Private** | No telemetry or network service. Content searches read files locally on demand. Everything stays in `%USERPROFILE%\.PathScout`. |
| ⬆️ **Self-updating** | Running from a git checkout? PathScout notices new commits and updates with one click. |

<div align="center">
<img src="docs/images/explorer-search.png" width="440" alt="Searching inside an Explorer folder">
</div>

Typing in Explorer opens a compact completion bar. Matching direct folders come
first, followed by direct files; only when there are no direct matches does it
search the catalog below that folder. Direct folder names are read live, including
folders containing `.nobackup` or `.no-backup`; their contents stay excluded from
indexing and recursive search. Clicking a folder or pressing Enter navigates the
original Explorer tab, without opening another window. Clearing the scope returns
to the global launcher.

## 🚀 Quick start

### With pipx (recommended)

[pipx](https://pipx.pypa.io/) installs PathScout into its own environment and adds
a `pathscout` command that starts without a console window:

```powershell
pipx install "pathscout[rust] @ git+https://github.com/bjspi/PathScout.git@v0.2.2"
pathscout --show
```

The `[rust]` extra downloads the matching Rust search engine wheel from the
GitHub release — no compiler needed. Update later by running the same command
with the new version's tag and `--force`. `pipx uninstall pathscout` removes the
program; your settings and catalog in `%USERPROFILE%\.PathScout` stay.

### From a checkout

```powershell
git clone https://github.com/bjspi/PathScout.git
cd PathScout
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[rust]"
.\.venv\Scripts\python.exe -m pathscout
```

`[rust]` adds the fast Rust search engine (needed for `contains:`) as a prebuilt
wheel from the matching [GitHub release](https://github.com/bjspi/PathScout/releases).
After `git pull` to a newer version, run the install line again. Without the
extra — or on a commit newer than the latest release — PathScout still works
with its slower Python engine and says so in the search bar; you can also
[build the engine yourself](#rust-search-engine).

Afterwards, start it without a console window:

```powershell
.\Start-PathScout.ps1               # start quietly in the tray
.\Start-PathScout.ps1 -Show         # start and open the search bar
```

PathScout starts with your Desktop, Documents and Downloads plus installed
applications. Add your work folders in **Settings → Search & indexing** — or just
drag them from Explorer into the list. The first scan runs in the background.

Tick **Start with Windows** in the tray menu or in Settings to launch PathScout at
sign-in. It manages only its own shortcut in your Startup folder.

### 📋 Requirements

- Windows 10 or 11
- Python 3.11 or newer with SQLite FTS5 (the standard python.org and Conda builds);
  the Rust engine wheel supports Python 3.11 to 3.14
- An ordinary user account — no administrator rights, services or drivers

## ⌨️ Keyboard

| Keys | Action |
| --- | --- |
| <kbd>Ctrl</kbd> <kbd>Ctrl</kbd> | Show or hide the search bar |
| <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Space</kbd> | Alternative global shortcut |
| Typing in Explorer | Complete direct folder/file names; search below only without a direct match |
| <kbd>↑</kbd> <kbd>↓</kbd> | Select a result |
| <kbd>Enter</kbd> | Open the selected result |
| <kbd>Ctrl</kbd>+<kbd>1</kbd> … <kbd>Ctrl</kbd>+<kbd>9</kbd> | Open the numbered result |
| <kbd>Ctrl</kbd>+<kbd>Enter</kbd> | Show the result in its folder |
| <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>C</kbd> | Copy the path |
| <kbd>Ctrl</kbd>+<kbd>D</kbd> | Toggle favorite |
| <kbd>Ctrl</kbd>+<kbd>L</kbd> | Select the query |
| <kbd>Ctrl</kbd>+<kbd>,</kbd> | Settings |
| <kbd>Shift</kbd>+<kbd>F10</kbd> / right-click | Open, reveal, copy, favorite, exclude |
| <kbd>Esc</kbd> | Hide (indexing keeps running in the tray) |

Click the icon at the right of the bar for the **All / Apps / Files / Folders**
filters, refresh, an expanded view, settings, About and restart. Right-click a
result → **Exclude this or a parent folder…** to drop a noisy folder tree; the
picker shows how many catalog entries each choice removes.

The three icons in the bottom bar restrict results to **Files / Folders / Apps**.
Click the selected icon again for **All**, or use **Alt+1 / Alt+2 / Alt+3** and
**Alt+0** for All. Typed `type:file`, `type:folder` (or `type:dir`) and `type:app` filters update
these buttons; choosing a button replaces any typed kind override.
Use `t:` as shorthand for `type:`, for example `t:dir`, `t:file` or `t:app`.

## 🔍 Query language

```text
report                                  name or path contains "report"
vsc type:app                            initials of an application
*.json                                  shell-style filename wildcard
report[0-9].json                        one digit in the filename
report ext:pdf,docx                     only these extensions
ext:doc,docx                            Word document extensions
ext:doc*                                any extension starting with doc
ext:(jpg,png)                           either extension; parentheses are optional
budget in:"D:\Work Projects"            inside this folder
path:projects/reports name:annual       ordered parent folders, name only
report after:2025-06-01 sort:modified   changed since June 2025, newest first
size:>5mb size:<1gb                     files larger than 5 MB and smaller than 1 GB
report after:2026-02* before:2026-02*   reports modified during February 2026
report !archive                         exclude paths containing "archive"
report contains:"invoice number"        filter matching text files by their contents (Rust)
*.json contains:gsk_*                   fnmatch pattern within text contents (Rust)
*.json rg:gsk_*                         content: and rg: are aliases for contains:
report match:keyword score:match        no fuzzy matching, text relevance only
report low:true                         include low-priority results
%TEMP%                                  open the Windows temporary directory
%LOCALAPPDATA%                          open local application data
~/                                      list the user directory's direct children
~/.config                               open this folder even outside indexed roots
```

| Filter | Meaning |
| --- | --- |
| `type:file` · `type:folder` · `type:dir` · `type:app` | Item kind; `t:` is shorthand for `type:` |
| `ext:pdf,txt` · `ext:(jpg,png)` · `ext:doc*` | Extensions/patterns, combined with OR (repeatable) |
| `in:"C:\Folder"` | Descendants of an absolute folder |
| `path:a/b` | Parent folders in this order (repeatable) |
| `name:text` | Match the file name without extension |
| `!text` | Reject paths containing `text` |
| `match:keyword` · `match:fuzzy` | Matching style (default fuzzy) |
| `sort:score` · `sort:name` · `sort:modified` | Order (default score) |
| `score:combined` · `score:match` | With or without personal ranking |
| `after:YYYY-MM-DD` · `before:YYYY-MM-DD` | Modification date range, inclusive |
| `after:20260215` · `after:2026-W07-1` | Compact and other ISO 8601 day forms |
| `after:2026-` · `after:2026-02*` | From January 1 or February 1, 2026 |
| `before:2026-` · `before:2026-02*` | Through the end of 2026 or February 2026 |
| `size:>5kb` · `size:<2mb` | File/application size; also `>=` and `<=` |
| `contains:"some text"` · `contains:gsk_*` | Case-insensitive text/fnmatch content filter; needs the Rust engine |
| `content:text` · `rg:text` | Aliases for `contains:`, with identical fnmatch behavior |
| `low:true` | Show low-priority results alongside the others |

Quoted text keeps spaces, apostrophes are literal and Windows paths such as
`C:\Work` stay ordinary search text. The full contract is in the
[behavior specification](docs/specification.md).

Search terms support fnmatch patterns: `*`, `?`, `[abc]`, `[0-9]` and `[!0-9]`.
Patterns match the whole filename, including its extension; `name:` matches the
stem instead. Use `[[]` for a literal opening bracket. Patterns work inside
quotes, alongside ordinary fuzzy terms and with filters. `path:` accepts the
same patterns for individual parent folders; `!text` remains a literal exclusion.

Extension filters also accept fnmatch patterns. `ext:doc,docx` finds exactly those
extensions; `ext:doc*` also includes `docm` and other extensions starting with
`doc`. Lists accept optional parentheses and spaces inside them, such as
`ext:(jpg, png)` or `ext:(doc*, pdf)`. Repeated `ext:` filters add alternatives.

Size units are case-insensitive: `b`/`byte`, `kb`/`kbyte`/`KiB`, `mb`/`mbyte`,
`gb`/`gbyte` and `tb`/`tbyte` (plural `bytes` forms also work). Each step is 1024;
without a unit the value is bytes. Decimal points or commas work (`size:>1.5mb`),
and `size:> 5kb` is accepted. Repeated size filters intersect their bounds.
Folder sizes are not calculated. Dates accept every ISO 8601 day that Python's
`date.fromisoformat` reads (`2026-02-15`, `20260215`, week dates such as
`2026-W07-1`). Partial dates accept a year or year-month, optionally ending in `-`
or `*`; `after:` starts the period and `before:` includes its final day, using
local time.
Size filters show approximate sizes on each result row; date filters show the
stored modification date. These labels use catalog metadata without reading
files while painting the UI.

`contains:` reads supported text files on demand after filename/path filters and
exclusions, before the final result limit. It builds no content index and runs on
the search worker with cancellation. JSON, Markdown, TXT, logs, CSV, configuration
and common source-code files are supported, including UTF-8 and BOM-marked UTF-16.
Files are not decoded, so ASCII text also matches in legacy encodings such as
Windows-1252. Binary data, Office documents, PDFs, files over 64 MiB and cloud
placeholders that would have to be downloaded first (OneDrive "online-only") are
skipped; unreadable or deleted files also do not match. PDFs with a text layer
would require a separate text extractor; image-only PDFs would additionally need
OCR. Narrow the filename, extension or folder first for large collections.

**Settings → Search & indexing → Content search threads** sets how many
files are scanned concurrently (1–16, default 4). This applies to `contains:`,
`content:` and `rg:`; filename search still uses the catalog. The Rust engine uses
ripgrep libraries directly, with reusable workers and no per-file processes.
Higher values can increase disk activity and memory use. Save applies the change
to the next search without rebuilding the catalog.

Content patterns match anywhere within a line and support `*`, `?`, `[0-9]` and
`[!0-9]`. They do not span lines. Regex punctuation such as `.` and `+` stays
literal; use `[*]`, `[?]` and `[[]` to search for literal wildcard characters.

### 🧮 Calculator

When the query is a calculation, its result appears above the search results and
is selected: <kbd>Enter</kbd> or a click copies it, <kbd>↓</kbd> moves to the
results, which are searched as usual.

```text
200 + 10%              220      percent of the left side for + and -
50% * 30               15       elsewhere 50% is 0.5
2^10 · 2**10           1024     power; also × and ÷
1,5 * 3                4,5      decimal commas are answered with commas
sqrt(2) · exp(1) · ln(e) · log(1000) · log(8, 2)
sin(pi/2) · cos · tan · asin · acos · atan · deg(pi) · rad(180)
floor(2.7) · ceil(2.1) · round(2.5) · round(2.345, 2) · abs(-3) · fact(5)
min(4, 2, 9) · max(1,5; 2) · mod(10, 3)
bin(10) · hex(255) · oct(8) · 0xff · 0b101 · 0o17
chr(65) · ascii(10) · ord(A) · hex2ascii(30323030) · ascii2hex(0200)
```

Constants are `pi`/`π`, `e` and `tau`; names ignore case. `log` is base 10 unless
a base is given, `ln` is natural, and angles are radians. Multi-argument
functions separate arguments with `,`, or with `;` when the numbers use decimal
commas. A lone number or constant (`2024`, `pi`) and digits joined by hyphens
(`2024-06-15`, `2024-2025`) stay searches. The calculator parses the text itself
and never runs Python code; results larger than about 1000 digits are not shown.

### 📁 Paths and shortcuts

Start with `%` to suggest known Windows directory variables (`%APPDATA%`,
`%USERPROFILE%` and others). A trailing `/` or `\` lists direct children;
`~/Doc` completes names below your user directory. These shortcuts work without
indexing that folder, run in the background and honor exclusions and markers.
`in:%USERPROFILE%` also expands variables when filtering the existing catalog.

## 🎛️ Exclusions and priorities

**Settings → Exclusions** takes absolute folders (drag & drop works), file/folder-name
patterns with `*`, `?` and `[…]` wildcards, and marker file names. Defaults:

- **Markers:** `.nobackup`, `.no-backup`, `pyvenv.cfg` (Python environments) and
  `conda-meta` (Conda environments) exclude their folder and everything below it.
- **Windows shortcuts:** `*.lnk` excludes shortcuts in your folders, including existing
  catalog results. Start-menu and registered applications stay searchable: file-name
  patterns do not apply to them (folder exclusions and markers still do).
- **Office temporary files:** `~$*` excludes lock/owner files such as `~$Report.docx`.
- **Folder names:** `.git*`, `.svn`, `.hg`, `__pycache__`, `.venv`, `venv`,
  `.ruff_cache`, `.mypy_cache`, `.dmypy_cache`, `.pytype`, `.pyre`, `.pyright`,
  `.basedpyright`, `.pylint.d`, `.pylint_cache`, `.lint_cache`, `.pytest_cache`,
  `pytest-cache-files-*`, `.tox`, `.nox`, `.hypothesis`,
  `.ipynb_checkpoints`, `.eggs`, `*.egg-info`, `node_modules`, `.next`,
  `.parcel-cache`, `build`, `dist`, `.gradle`, `.cxx`, `.externalNativeBuild`,
  `.vs`, `.idea`, `$Recycle.Bin`, `System Volume Information`.

Patterns match whole names, so `buildings` stays searchable.

**Settings → Ranking** boosts or demotes folders, one rule per line:

```text
25  | D:\Projects | high
-20 | D:\Archive  | low
0   | D:\Private  | ignored
```

**Advanced rules** add filename-regex, extension and Windows-attribute rules.
`ignored` hides an item completely, `low` makes it a fallback, `high` lifts it to
the top group; the number fine-tunes the order.

<div align="center">
<img src="docs/images/settings.png" width="520" alt="PathScout settings">
</div>

## 🔒 Privacy

- 🏠 Runs entirely on your computer. No account, telemetry or cloud service.
- 📄 Indexes names and metadata only — never file contents.
- 🗂️ Settings, catalog, favorites, history and logs live in `%USERPROFILE%\.PathScout`
- 📝 `settings.json` holds user preferences. `pathscout.log` rotates at 5 MB with five backups.
  Diagnostics record search timing, UI stalls, shortcut dispatch and focus owners
  (process names/PIDs, without query text or window titles). Log writes run in the background.
  Existing settings, favorites and history from `%LOCALAPPDATA%\PathScout` are copied
  on first start; originals are retained. Close an older running instance first.
  and contain your paths and queries; treat that folder like other personal data.
- 🧹 **Clear search history** in the tray forgets launches but keeps favorites.

## 🛠️ Command line

```powershell
python -m pathscout                         # start in the tray
python -m pathscout --show                  # start and open the search bar
python -m pathscout --no-hooks              # no global shortcuts or Explorer typing
python -m pathscout --data-dir D:\Temp\Try  # separate settings and catalog
python -m pathscout --version
```

PathScout starts quietly in the tray; press <kbd>Ctrl</kbd> <kbd>Ctrl</kbd> to search.
Starting it again opens the running instance's search bar, unless the second start
uses `--background`.

## 🧩 How it works

```mermaid
flowchart LR
    Keys["⌨️ Keyboard hook<br/>+ Explorer context"] --> UI["🪟 Search bar"]
    UI --> Search["🔍 Parser · filters<br/>matching · ranking"]
    Search --> DB[("🗄️ SQLite + FTS5<br/>catalog")]
    Watch["👀 Folder watcher"] --> Index["📥 Indexer"]
    Index --> DB
```

Scans are staged and published atomically, so a cancelled or failed scan never
empties the catalog. Unreadable folders keep their previous entries while the rest
refreshes. Each search runs in the background and is cancelled as soon as you type
on. Read the [architecture notes](docs/architecture.md) for the details.

## 🧪 Development

### Rust search engine

The separate `pathscout_native` package is PathScout's search engine: matching,
ranking, SQLite candidate reads and `contains:`. The Python ranking stays the
behavior reference, and the test suite checks that both produce identical scores,
reasons and order. The PathScout package itself needs no Rust dependencies; when
the extension is missing or does not fit this Python, PathScout searches with
the Python engine, shows why in the search bar and leaves `contains:` unavailable.

One abi3 wheel serves Python 3.11 to 3.14; it picks the Unicode tables of the
running interpreter (Unicode 14.0, 15.0, 15.1 and 16.0). Build it with Rust stable
for `x86_64-pc-windows-msvc` and Visual Studio Build Tools with **Desktop
development with C++**:

```powershell
python -m pip install maturin
python native\tools\build.py --out dist            # abi3 wheel without local paths
python -m pip install --force-reinstall (Get-ChildItem dist\pathscout_native-*.whl).FullName
python -c "import pathscout_native; print(pathscout_native.API_VERSION)"
```

`native/tools/build.py` wraps `maturin build --release --locked` and remaps your
user folder, the checkout and the Cargo/rustup homes, so the wheel contains no
paths from your machine. After changing `native/Cargo.lock`, refresh the shipped
license texts with `python native\tools\collect_licenses.py`; after adding a
Python version, generate its Unicode tables with
`python -I native\tools\generate_unicode.py` using that interpreter.

`PATHSCOUT_SEARCH_ENGINE` selects the engine for tests and diagnostics: `auto`
(default) uses Rust when available, `python` forces the reference engine, and
`native` requires the extension and fails instead of falling back. In `auto`, an
unexpected Rust error is logged once and that search is repeated in Python.
Filename regex rules run natively for ASCII names; other names, and syntax Rust's
regex cannot match exactly, call the rule's compiled Python pattern instead.

Compare the engines and both native phases:

```powershell
python tools\benchmark.py --engine python --entries 50000 --directories 1000
python tools\benchmark.py --engine native --phase rows --entries 50000 --directories 1000
python tools\benchmark.py --engine native --phase sqlite --entries 50000 --directories 1000
cargo test --manifest-path native/Cargo.toml --locked
```

`PATHSCOUT_NATIVE_PHASE=rows` keeps SQLite reads in Python; the default `sqlite`
phase streams candidates in Rust.

### Tests and checks

```powershell
python -m unittest discover -s tests -v          # unit, UI and integration tests
python tools\smoke_test.py --native              # real Qt flow + hook attach/detach
python tools\benchmark.py --entries 50000        # synthetic search benchmark
python tools\readme_screenshots.py              # regenerate the README screenshots
python -m ruff check pathscout tests tools native/tools
python -m ruff format --check pathscout tests tools native/tools
```

Tests use temporary catalogs, folders and git repositories; they never touch your
real settings, Startup folder or files.

### GitHub builds and releases

[CI](https://github.com/bjspi/PathScout/actions/workflows/ci.yml) runs on pushes to
`main`, pull requests and manual dispatch. It checks Python style, Rust formatting
and Clippy, runs the Windows tests with each engine, tests the Rust wheel on
Python 3.11, 3.12, 3.13 and 3.14, checks the Qt/keyboard flow, and builds and
verifies the Python wheel/source distribution and the Windows x64 Rust wheel. Download them from the run's `pathscout-windows-packages` artifact.

Pushing a `v*` tag runs the same checks and publishes a GitHub Release only after
they pass, attaching those packages and a clean source ZIP. The tag must match
`pathscout/__init__.py`, `pyproject.toml`, `native/pyproject.toml` and
`native/Cargo.toml` (for example `v0.2.2`), and the `rust` extra in `pyproject.toml`
must point to that release's native wheel. Install both wheels from a release
with `python -m pip install <python-wheel> <native-wheel>` to enable Rust without
installing a compiler. Source ZIPs retain `native/` for local builds.

For example, publish version 0.2.2 from a clean, committed checkout with:

```powershell
git tag -a v0.2.2 -m "PathScout 0.2.2"
git push origin v0.2.2
```

The finished release appears under [Releases](https://github.com/bjspi/PathScout/releases).

📚 [Architecture](docs/architecture.md) ·
[Specification](docs/specification.md) ·
[Third-party notices](THIRD_PARTY_NOTICES.md)

## ⚠️ Limitations

- Windows only: the keyboard hook, Explorer integration, folder watching and
  autostart use Windows APIs.
- Paths longer than 260 characters are indexed and searched, but some programs
  cannot open them; Explorer and most current applications can.
- Indexes by walking folders and watching changes, not by reading the NTFS
  journal; very large drive-wide catalogs build slower. Without the Rust engine,
  broad one-letter searches over 50,000 entries take around half a second.
- Input methods (IME), Windows 11 tab edge cases, high-DPI multi-monitor setups and
  screen readers still need more real-world testing.

## 📄 License

[MIT](LICENSE) © PathScout contributors. Dependencies keep their own licenses — see
[third-party notices](THIRD_PARTY_NOTICES.md).
