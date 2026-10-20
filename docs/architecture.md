# Architecture

## Domain boundaries

PathScout is organized around catalog indexing, filesystem change monitoring,
query parsing, filtering, text matching, ranking, platform integration and UI state.
Value objects carry intent and policy; adapters own I/O. There is no service
locator, dependency-injection framework or shared database connection. The
controller composes the application explicitly.

```mermaid
flowchart TD
    Window[LauncherWindow search bar and SettingsDialog] -->|intents| Controller[LauncherController: composition]
    Tray[TrayMenu] -->|intents| Controller
    Keys[KeyboardListener and ExplorerTracker] -->|KeyboardIntent queue| Controller
    Watch[DirectoryWatcher] -->|WatchEvent queue| Controller
    Controller --> Session[SearchSession: debounce, cancel, Enter]
    Controller --> Scheduler[IndexScheduler: one job at a time]
    Controller --> Status[StatusBoard: activity and problems]
    Session --> Search[SearchService]
    Scheduler --> Index[CatalogIndexer]
    Search --> Candidates[candidates: query to SQL]
    Search --> Ranking[ranking: filters, matching, personalization, priority]
    Index --> Rules[ExclusionPolicy and PriorityPolicy]
    Search --> Rules
    Candidates --> Store[CatalogStore: SQLite and FTS5]
    Index --> Store
    Controller --> Actions[ItemActions] --> Store
    Actions --> Shell[Windows shell]
```

| Module | Responsibility |
| --- | --- |
| `catalog/model.py` | Catalog values, `CatalogSource`, path identity (`path_key`) versus search normalization (`normalize`) |
| `catalog/exclusions.py` | Excluded paths, file/folder patterns and marker files, cached per operation |
| `catalog/priority.py` | Search classes (ignored/low/normal/high) and numeric boosts |
| `catalog/sources.py` | Which trees the catalog owns and which source owns a path |
| `catalog/indexer.py` | Scan policy, traversal, staging, reporting and publication decisions |
| `catalog/store.py` | SQLite schema and migration, staged snapshots, usage and favorites |
| `search/query.py` | The immutable parsed `SearchQuery` |
| `search/query_parser.py` | Tokenizing, recognized filters, value parsing and validation |
| `search/filters.py` | Structured eligibility: type, extension, scope, exclusions, dates |
| `search/matching.py` | Text match quality, ordered folder matching and fuzzy alignment |
| `search/ranking.py` | Eligibility, text relevance, personalization, configured priority, explanation |
| `search/candidates.py` | Query-to-SQL translation for the literal and fuzzy candidate passes |
| `search/service.py` | Streaming candidates into a bounded best-hits heap |
| `settings.py` | Validated settings value object and atomic JSON file |
| `actions.py` | Open, reveal and favorite use cases; only successful opens are learned |
| `platform/` | Win32 bindings, keyboard hook, Explorer COM, directory watching, shell, autostart |
| `platform/file_dialog.py` | Quick Switch: read an Open/Save dialog's folder and drive its file-name box |
| `ui/` | Search bar window, result icons and highlighting, settings, exclusion picker and About dialogs, tray, status, search session, index scheduler, release updater, worker delivery |
| `platform/git.py` | Read-only git queries for self-updating a source checkout |
| `releases.py` | Release tags, installation kinds and their update commands |
| `install.py` | The reinstall that finishes a checkout update at the next start |
| `__main__.py` | CLI, logging, crash logging, per-data-directory single instance and restart handover |

`catalog/` and `search/` do not import Qt or Win32. Their behavior is tested with
temporary SQLite catalogs and ordinary filesystem fixtures. The search package
translates queries into SQL itself (`candidates.py`), so the catalog store does not
depend on the query language.

## Catalog indexing

One scan is split along its responsibilities:

- `_EntryRules` decides whether an entry belongs in the catalog: links and offline
  folders, exclusions and ignored priorities. A scoped scan applies the same rules
  to every ancestor up to its source root (`allows_scope`). Disk errors propagate.
- `_ScanSession` walks directories. Entries stream from `os.scandir`; traversal
  state is a local pending-folder list. Each visit returns the subfolder to descend
  into, so no mutable state is passed through parameters.
- `catalog/long_paths.py` gives every `stat`/`lstat`/`scandir` call the
  extended-length form for paths of 240 characters or more and turns entry paths
  back into ordinary ones, so the catalog never stores the `\\?\` prefix.
- `_StagedRows` batches accepted items (500 per batch) into a private temporary table.
- `_ScanOutcome` records skips, error messages, paths whose old rows must survive,
  and unavailable sources.
- `CatalogIndexer` decides what a completed scan replaces: a full publication,
  one subtree, or an exact set of files.

A cancelled scan returns before publication, so the previous catalog stays
unchanged. An unreadable path keeps its previous rows while readable siblings still
refresh and delete. A wholly unavailable source keeps its previous snapshot. Index
marker probe failures preserve old rows; search fails closed on the same failure.
When an Explorer scope contains other configured sources, the scan keeps their
rows and indexing modes instead of taking ownership. An explicitly chosen root may
be a junction; links below a root are never followed.

WAL lets searches read while a scan stages. Each operation owns its SQLite
connection. Schema version 3 migrates versions 1 and 2 in one transaction, merging
alternate Windows path spellings without losing launches or favorites. Identity
uses absolute paths with nonexpanding uppercase mapping; search uses NFC/casefold.

## Search execution

`parse_query` collects tokens into a mutable draft; each recognized filter is one
assignment with its own value parser and user-facing error message. `build`
validates once and returns the frozen `SearchQuery`. Unknown `key:value` tokens and
Windows paths remain search text.

`read_candidates` builds SQL with `_SqlFilter`, which keeps every clause together
with its parameters. The literal pass uses FTS5 trigram matching for terms of three
or more characters and `LIKE` for shorter terms. The fuzzy pass matches
subsequences and excludes everything the literal pass returned. Both passes are
supersets of the domain matcher; `rank` makes the final decision:

1. Structured filters and ignored priority decide eligibility.
2. Ordered folder matching and per-term text matching decide text relevance.
3. Combined scoring adds bounded personalization and configured priority.
4. The priority band (promoted, normal, fallback) and an explanation are attached.

Ranking weights are named module constants. A `Ranker` prepares one query
(filters, scope identity, timestamp) and ranks many candidates. Candidates are
plain catalog rows with the stored normalized text; `Path` objects and hits are
created only for displayed results. `SearchService` streams candidates into a
bounded heap; once it is full, a candidate whose best possible sort key cannot
beat the weakest kept hit is skipped before text matching, and disk-based marker
checks run only for hits that would be kept. Empty default queries (optionally with a type filter) read only
learned items through the path identity index.

## Rust search engine

Explicit environment/home shortcuts are handled first by `search/navigation.py`:
one directory is enumerated on the search worker, with existing exclusions,
priority/ranking rules and result limits. No catalog or Rust candidate read is
needed. Known directory variables are suggested without exposing arbitrary
environment values; `in:` expands variables for catalog queries as well.

`search/native_engine.py` imports the extension once (`native_availability`),
checks `API_VERSION` and selects the Unicode tables of the running interpreter
with `use_unicode(unicodedata.unidata_version)`. The reason it is unavailable is
shown in the search bar. `PATHSCOUT_SEARCH_ENGINE=auto|python|native` selects the
engine for tests and diagnostics. The adapter builds one query specification and
one dict-based priority specification from the Python domain objects, and
reconstructs only the kept `Candidate`/`Ranking` objects. The Python loop in
`SearchService._rank_python` remains the reference and the fallback; execution
failures are logged once per exception type, and `native` re-raises them.

The separate `native/` maturin distribution exports `rank_rows` (Python-fetched
rows) and `rank_sqlite` (read-only SQLite, bundled FTS5, WAL, 15-second busy
timeout). Both share matching, priorities, ranking, bounds and a bounded heap.
`candidate_sql` remains the sole SQL builder. Literal and fuzzy statements are
combined with ordered `UNION ALL`, renamed named parameters and one heap; the
native SQLite reader holds one read transaction. No indexing or UI code moves.

Rust releases the GIL during ranking and SQLite reads. It reacquires it only for
the exclusion callback `(path, is_folder, from_application_source)`, after the
same eligibility, bound and heap checks as Python, and for filename rules it
cannot match natively. Atomic cancellation is checked on every candidate and SQLite's progress
handler; a short-lived bridge forwards the existing Python `Event` every 5 ms.
Phase-one row conversion yields the GIL in batches. Cancellation returns no hits.

The abi3 wheel carries Unicode tables for Python 3.11 to 3.14 (Unicode 14.0, 15.0,
15.1 and 16.0): full casefold, character classes, canonical decompositions and
compositions, generated by `native/tools/generate_unicode.py` from each
interpreter's `unicodedata`. Rust implements NFC itself; per-version checksum
tests cover every scalar value and 60,000 generated strings. Sorting uses direct
full casefold without NFC, as Python does. Filename rules use `fancy-regex` for
ASCII names; for other names, or syntax Rust cannot match exactly, Rust calls the
rule's compiled Python pattern, so a search never switches engines midway.
Scores are added left to right in both engines and the personalization uses one
explicit log2 formula, because `sum()` and `math.log2` round differently across
Python versions. Parity tests compare scores, bands, reasons, order and callback sequence
against the Python loop and exercise both extension entry points.

## Threads, scheduling and cancellation

Qt widgets live on the GUI thread. `BackgroundTasks` runs work on separate search
(2), index (1) and action (1) pools and always delivers results through a queued
signal to callbacks on the GUI thread. Callbacks never run inside `submit`.

`SearchSession` debounces typing, cancels the previous search, discards results of
abandoned searches by identity, and implements the Enter contract: Enter opens a
result of the query currently typed, waiting for its results when necessary.
Hidden windows do not rerun searches.

`IndexScheduler` runs one index job at a time. Pending work is prioritized: a full
scan, then the broadest dirty folder, then a batch of changed files. Repairs never
postpone the single-shot periodic full scan. While an Explorer-scoped session is
visible, full scans wait so its temporary snapshot survives. Changing indexing
settings cancels the running job, drops queued repairs and schedules a full scan.
UI-only settings restart nothing.

The keyboard listener thread runs a Windows message loop with a `WH_KEYBOARD_LL`
hook and the Ctrl+Alt+Space hotkey. Its callback reads a cached Explorer context
maintained by a separate COM apartment thread and queues `KeyboardIntent` values.

`DirectoryWatcher` threads own one directory handle, one event and one buffer.
`_PendingRead` guarantees that an overlapped `ReadDirectoryChangesW` call is
cancelled and acknowledged by the kernel before its buffer can be released.
Notification batches become `FileChange` (exact metadata refresh) or
`FolderChange` (subtree reconciliation). Overflow repairs the root. A failing watch
reports `WatchFailed` once, retries every 30 seconds, and reports `WatchRestored`
when it works again; the controller then repairs the whole root because changes
during the outage were never reported.

## Platform integration

PathScout uses an out-of-process low-level keyboard hook, pointer-sized ctypes
declarations, and `Shell.Application` to resolve a filesystem folder. It accepts
typing only in a recognized Explorer file view and never injects code into Explorer.
After a dead key, the next key stays with Explorer so accent composition is not broken.

Quick Switch treats Open and Save dialogs of other programs like Explorer tabs.
The `ExplorerTracker` also resolves a focused dialog file list, reading the folder
from the dialog's breadcrumb toolbar text, and remembers the most recently focused
Explorer tab, whose folder it reads again while a dialog is in front. Both reach
the hook as cached values. `ExplorerTyping` carries a `ViewKind`, so the controller
opens the same compact bar and routes a chosen result to
`ItemActions.choose_in_dialog` instead of `navigate_to`; Ctrl+G queues a
`FolderJumpRequested`. `platform/file_dialog.py` drives the dialog on the action
worker with `WM_GETTEXT`, `WM_SETTEXT` and the default button's `WM_COMMAND`, all
with timeouts, so a hung dialog cannot block PathScout.

`StartupShortcut` manages only `PathScout.lnk` in the user's Startup known folder.
The shortcut file is the source of truth for the tray and the settings dialog. It
starts `pythonw.exe` of the running environment with
`-m pathscout --background --data-dir <dir>`, quoted with `list2cmdline`, in the
directory that contains the `pathscout` package (a source checkout root or
site-packages). Shortcuts without PathScout's description and arguments are
reported as unrelated and never changed. COM objects are released before the
apartment is uninitialized.

A per-data-directory `QLockFile` elects one primary process before it opens a
user-restricted local server. A second foreground start asks the primary to show
the launcher; a second `--background` start only confirms it is running.
The default data directory is `Path.home() / ".PathScout"`. Startup copies existing
settings and catalog/history/favorites from the former LocalAppData directory
without removing the originals or replacing existing new files. SQLite's backup
API includes WAL data; a legacy instance lock prevents copying during an active
session. Old Startup shortcuts naming the former default are redirected to the
new directory. Other explicit `--data-dir` paths remain isolated.

Only the instance-lock owner opens `pathscout.log`. A standard-library
`QueueHandler`/`QueueListener` writes UTF-8 logs outside the GUI/keyboard threads,
rotating at 5 MB and keeping five backups. Startup, search engine/timing,
shortcut dispatch delay, settings changes, focus transitions and event-loop gaps
over 250 ms are recorded, along with unhandled exceptions. Focus logs identify
the foreground executable/PID/class without window titles or query text.

Shell icon queries run on one daemon worker and return `QImage`s through a queued
Qt signal; only the GUI thread creates pixmaps. Painting uses a generic icon until
the cached shell icon arrives, so a slow shell extension cannot block input or
shutdown. Hiding the launcher cancels pending and active searches. Windows focus
requests are verified; a denied request gets one attempt with the foreground input
queue temporarily attached, always detached in `finally`. A later focus change is
logged without repeatedly pulling focus away from another application.

The updater's post-fetch revision count also runs through QProcess, keeping
periodic Git work off the GUI thread. `GCM_INTERACTIVE=Never` and
`GIT_TERMINAL_PROMPT=0` prevent background credential dialogs/prompts.

## Operational choices

- Local metadata only; no file-content reads for search and no external service.
- Explicit roots rather than an automatic administrative whole-volume scan.
- SQLite/FTS5 rather than another database dependency.
- Qt Widgets via PySide6; Windows integration via ctypes and pywin32.
- No elevation, shell extension, service or scheduled task. Autostart is an
  ordinary per-user Startup shortcut created only on request.
- Original Python implementation and artwork.
