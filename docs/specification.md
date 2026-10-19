# Behavioral specification

## Catalog

The catalog records absolute path, normalized name/path, item kind, size,
modification time and Windows attribute bits. Executables and `.lnk`, `.url` and
`.appref-ms` files are applications; other ordinary files are files. Directory
entries are folders. Application display names omit their extension.

Configured roots are recursive. With application discovery enabled, Start menu
directories are scanned for launchable entries and HKCU/HKLM App Paths are read in
both registry views. This does not enumerate the UWP AppsFolder namespace.

An explicitly selected root may be a symbolic link or directory junction. Links,
junctions and offline directories encountered below that root are skipped. Metadata
for ordinary files, including some cloud placeholders, can be
indexed without reading contents. Access failures appear in the scan report and
preserve entries under the failed path; readable siblings still reconcile deletions.
A wholly unavailable root retains its snapshot. A missing item may therefore remain searchable until a
successful refresh; attempting to open it reports an error and does not learn a
launch.

Scans publish atomically. Cancellation must leave the previous catalog unchanged.
A changed subtree refresh must preserve unrelated entries. On startup, a refresh
is requested; file metadata notifications trigger exact-file refreshes, structural
changes trigger subtree refreshes, and the default periodic full
refresh interval is 300 seconds. The interval is configurable from 30 to 86,400
seconds after the previous full scan completes. Requests queue behind running scans;
partial repairs cannot postpone the periodic full-scan deadline.
Directory metadata-only notifications wait for periodic refresh; they do not trigger
recursive scans of their parents. A scoped scan containing separate configured
sources retains their snapshots and indexing modes. Hiding an Explorer search
inside an existing source does not request an additional full scan. When a failed
folder watch works again, its whole root is repaired, because changes during the
outage were not reported. A watch failure is reported once per outage, with the
Windows error, and stays visible in the launcher, the tray tooltip and the index
status until the watch is restored.

## Exclusions

An explicit directory exclusion applies to itself and every descendant, with path
component boundaries (`cache` does not exclude `cache-copy`). Matching uses absolute,
nonexpanding uppercase path identities. Search text independently uses NFC/casefold
normalization. Alternate Windows casing retains favorites and usage; Unicode names
that merely share search normalization remain separate identities. Case-sensitive
NTFS directory namespaces are not supported.

Patterns without a separator match file and directory names; `*`, `?` and bracket patterns
use Python fnmatch semantics. Patterns with `/` or `\` match the full normalized
absolute path using `/` separators. Matching directories exclude their subtree.
For example, `*.lnk` excludes Windows shortcuts by default. Filename/extension
priority rules with `search_priority: "ignored"` can also exclude files by name or type.

A configured marker name found in any ancestor directory excludes that directory
and its descendants, including when the ancestor is above a scan root. A marker
is detected by existence; PathScout does not modify it. Defaults are `.nobackup`,
`.no-backup`, `pyvenv.cfg` (Python virtual environments) and `conda-meta` (Conda
environments); a marker may be a file or a directory. Default folder-name patterns
exclude version-control metadata (`.git*` also covers copies such as `.git_old`,
and `.github`), Python/JavaScript caches and environments,
build output (`build`, `dist`, Gradle/Android/CMake folders), IDE state and Windows
system folders; the README lists them. Excluded paths never gain eligibility from a favorite, history
or priority boost. Search rechecks current exclusions rather than trusting only
the last scan. If marker metadata cannot be checked, search hides the affected path
and indexing preserves its old snapshot while reporting the error. Marker creation triggers subtree cleanup where a watcher is active;
marker removal allows reindexing. Roots excluded when monitoring starts rely on the
periodic refresh to rediscover removal of their marker.

## Query language

Queries are limited to 512 characters, terms to 128 characters; control characters
and invalid operator values are rejected with a message naming the operator and its
accepted values. Double-quoted tokens preserve spaces; an empty quoted token (`""`)
is ignored; apostrophes are literal characters. Repeated `ext:` and `path:` filters
accumulate; size comparisons intersect; for other filters the last value wins. Unsupported dates return a validation error. Windows
backslashes remain literal. Matching is NFC-normalized and case-insensitive.

| Expression | Contract |
| --- | --- |
| `annual report` | Every term must match the name or literal full path |
| `"annual report"` | One term containing a space |
| `*.json`, `report?.json`, `report[0-9].json` | Whole-filename fnmatch patterns, including the extension |
| `type:file`, `type:folder`, `type:app` | Restrict item kind; overrides the UI kind button |
| `ext:pdf,txt` | Any listed extension, case-insensitive, optional leading dots |
| `in:"D:\Work Projects"` | Restrict to descendants of this absolute folder |
| `path:projects/reports` | Match parent folder components in that order; intervening components allowed |
| `name:annual` | Match the stem only, excluding the extension and parent path |
| `!archive` | Reject a literal substring anywhere in the full path |
| `match:keyword` | Exact/prefix/word-prefix/substring matching; no fuzzy subsequences or initials |
| `match:fuzzy` | Also permit initials and ordered, noncontiguous name characters (default) |
| `score:combined` | Text relevance plus personalization and configured boosts (default) |
| `score:match` | Text relevance only; eligibility and low fallback still apply |
| `sort:score` | Search class band then descending relevance (default) |
| `sort:name` | Ordinary before low results, then alphabetical name |
| `sort:modified` | Ordinary before low results, then newest modification |
| `after:2025-06-01` | Modification on or after local midnight on this day |
| `before:2025-06-30` | Modification through the end of this local day, inclusive |
| `after:2026-`, `after:2026-02*` | Start of the specified local year/month (January 1 / February 1) |
| `before:2026-`, `before:2026-02*` | Through the last day of the specified local year/month |
| `size:>5kb`, `size:<2mb` | Strict file/application size comparison; `>=` and `<=` include the boundary |
| `contains:"invoice number"` | Literal, case-insensitive text content filter; Rust backend required |
| `low:true` | Include low results even when ordinary/high results exist |

An Explorer session supplies the default folder scope. An explicit `in:` overrides
it. The scope directory itself is excluded from its descendant results. Folder-path
matching uses the selected keyword/fuzzy matcher on each parent component. Unknown
prefixes remain literal text, which allows ordinary paths containing colons.

Size units use powers of 1024, case-insensitively: bytes (`b`, `byte`, `bytes`),
`kb`/`kbyte`/`kbytes`/`KiB`, and equivalent `m`, `g` and `t` prefixes. No unit means
bytes. Decimal points and commas are accepted; byte bounds are rounded exactly
according to the comparison. Spaces after `size:` or its comparison are allowed.
Repeated comparisons form an intersection; invalid units, out-of-range byte bounds
and empty ranges report validation errors. Folders are excluded by size filters;
no recursive folder-size calculation is performed. SQL, Python and native ranking
apply the same inclusive integer byte bounds. Old native API versions fall back
to Python until rebuilt, so newly added filters cannot be silently ignored.

Dates accept `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, with an optional trailing `*` or
`-`. Missing parts expand to the first day for `after:` and the last day for
`before:`; leap years and local midnight boundaries are respected.

Fnmatch patterns support `*`, `?`, character sets/ranges and `[!...]` negation;
`[[]` escapes an opening bracket and an unmatched `[` is literal. `*` has no
special recursive meaning. Quoting preserves patterns. Matching uses the same
normalization as ordinary terms, with either slash spelling accepted in paths.
Without a separator, patterns match only the display name or actual filename,
never a parent folder. `name:` matches the stem; `path:` matches ordered parent
components. Patterns are not fuzzy, but accompanying ordinary terms may be.
The Python backend uses standard-library fnmatch; Rust uses fnmatch-rs with
regexes compiled once per query. SQL only supplies a safe candidate superset.
Excluded terms remain literal substrings.

Rows show approximate catalog size when a size filter is active and the local
modification timestamp when a date filter is active, including in Explorer mode
and accessible text. Painting performs no filesystem metadata reads. Creation
timestamps are not stored or used by the modification date filters.

Content search uses ripgrep's `grep-regex` and `grep-searcher` crates, with one
literal matcher compiled per query and a reusable streaming buffer. It applies
after ordinary ranking and exclusion checks, before a candidate enters the result
heap. Candidates beyond the result limit remain eligible, so an early set of
content mismatches cannot hide later matches. Contents are read afresh without a
content index. Only allowlisted text/source/configuration extensions and common
extensionless text names are eligible. Files must be regular, locally available
and at most 64 MiB according to current filesystem metadata. BOM-marked UTF-16 is
decoded automatically; NUL detection skips binary input. Office/PDF/media and
executables are not parsed. Unreadable, deleted or unsupported files do not match.
Read chunks check cancellation, and scanning runs with the Python GIL released.
`contains:` requires nonblank text of at most 128 characters; regex/glob operators
are literal. Repeated filters keep the last value. Python mode or an incompatible
extension reports a clear error; it never silently drops the content constraint.

The footer exposes Files, Folders and Apps toggles, also reachable with Alt+1,
Alt+2 and Alt+3. Clicking the active toggle again or using Alt+0 restores All.
Typed kind filters update toggle/menu state. Selecting a toggle removes typed
`type:` overrides while preserving other query tokens and quoted values.

Leading `%VARIABLE%` and `~`/`~/`/`~\` enter direct path navigation. An exact
existing path returns the file/folder; a trailing separator lists its direct
children, and an incomplete last component completes names by prefix. `%` and
incomplete variable names suggest known Windows directory variables, deduplicating
aliases with the same target. Unknown completed variables and relative expansion
values report an error. Resolution and enumeration run on a search worker, obey
type, priority and exclusion rules, support cancellation and the result limit,
and do not recursively scan or add entries to the catalog. Filesystem reads are
skipped for excluded subtrees. `in:` also expands environment/home variables.

## Search classes and boosts

Folder, filename regex, extension and Windows attribute rules produce a search class
and a numeric boost independently. Filename regex rules specify whether they apply
to files/applications, folders, or both. Attribute masks match when all bits in the
mask are present. Hidden = `2`, system = `4`, offline = `4096` are examples.

Among folder rules only the most specific matching path contributes. All matching
nonfolder rules contribute. Their numeric weights are summed. Every individual
weight must be an integer from -100 to 100.

Class combination is deterministic:

1. Any `ignored` rule makes the item ineligible.
2. Otherwise, any `low` rule makes it a low-priority fallback.
3. Otherwise, any `high` rule puts it in the high class.
4. Otherwise, it is normal.

Ignored folders are pruned during traversal. Existing rows affected by a newly
ignored folder-name rule are fully removed on refresh; filename rules are not
inherited by unrelated descendant filenames during search. Use an explicit excluded
path or folder priority when immediate subtree suppression is required.

In combined score order, high results precede normal results regardless of numeric
score; low results follow both. With the default `low:false`, low results appear
only when there are no normal/high matches. In text-only mode high and normal share
one band; explicit alphabetical/date order also ignores their distinction. The
configured display limit, 10–200 (default 60), is applied after candidate ranking.

## Independent relevance formula

These weights are PathScout's own, chosen and tested independently.
For each positive term, the first matching rule supplies its text score:

| Match | Score |
| --- | --- |
| Exact name or stem | 150 |
| Name prefix | 110 |
| Word prefix | 90 |
| Name substring | `75 - 0.05 × starting offset` |
| Initials / camel-case abbreviation | 65 |
| Literal full-path substring | 35 |
| Fuzzy name subsequence | 5–60 |

Fuzzy matching requires at least two characters and preserves character order. Its
dynamic program rewards consecutive characters and word/camel-case boundaries,
penalizing gaps and late starts. It runs in O(term length × name length).

Combined scoring adds:

- Applications: 12.
- Favorite: 80.
- Total successful launches: `min(24, 4 × log2(launches + 1))`.
- Successful launches for the query key: `min(35, 7 × log2(query launches + 1))`.
- Recency: `12 / (1 + age in days / 7)` when previously launched.
- Sum of applicable numeric priority weights.
- High search class: 20, in addition to its sorting band.

An empty query with default options shows only favorites and previously launched
items, using these nontext signals; an optional type filter still restricts that
learned set. An explicit constraint such as `ext:pdf` can browse unlearned entries
without positive text. `score:match` removes all those bonuses.
Ties use case-insensitive display name then absolute path. A favorite or explicit
boost can outrank a stronger text match in combined mode. Score explanations are
available in tooltips and expanded view.

Only a successful shell open records a launch. The learning key consists of
normalized positive terms, effective item kind, sorted extension filters and ordered
folder components. Scope, exclusions, score/match mode and date filters are omitted,
so related searches share history. History clearing preserves favorites. No query
is sent to a remote service.

## Input and actions

Double Ctrl requires two short Ctrl press/release pairs within the configured
interval (default 350 ms, configurable 150–800 ms). Repeats, long holds and Ctrl
chords invalidate the gesture. Ctrl+Alt+Space is an alternate registered hotkey.

Explorer typing starts only when a recognized file view has focus and a unique
filesystem folder can be resolved. Address bars, search fields, rename fields,
modifier combinations, menus, drag/move operations and injected input pass through. The triggering printable
character is queued to the launcher. Subsequent text is entered into the focused
Qt query field. Virtual shell folders and ambiguous tabs are not intercepted.
After a dead key, the next key is left to Explorer, so accented type-ahead remains
Explorer's. IME composition needs target-system validation.

Open uses the Windows file association or shortcut; reveal selects the result in
Explorer. Applications receive their containing directory as the working directory.
Clipboard actions copy path, filename or parent path. Enter during an outstanding
query waits for matching results rather than opening an older result; a new query
selects its top result. These actions accept
filesystem entries rather than shell commands; PathScout does not implement a
command interpreter. Excluding a result's directory asks for confirmation showing
the exact directory, updates settings and refreshes the catalog. Unknown advanced-rule
JSON keys are rejected instead of silently dropping the user's rules.

## Launcher

The launcher is one compact bar: the query field, an optional folder-scope chip and
the application icon, which opens a menu with the type filters (All, Apps, Files,
Folders), Refresh catalog, Expanded view, Settings, About, Restart and Hide. The bar grows with up
to nine results (fourteen in expanded view) and shrinks back to the bare query
field for an empty query without history. Rows show the shell icon, the name with
matched characters highlighted, the containing folder and a `Ctrl+1`…`Ctrl+9`
shortcut that opens that row. Messages (no matches, invalid query, failures),
the result count and persistent problems appear below the results only when
present. The bar can be dragged by its empty areas.

Typing in Explorer opens a smaller one-line variant docked near the bottom of that
Explorer window; it grows upwards and shows each result's subfolder relative to the
searched folder. With a folder scope (Explorer or `in:`), the folder's direct
entries are listed before deeper matches; relevance orders each group, and
priority classes still come first.

The result context menu offers **Exclude this or a parent folder…**. A picker lists
the result's folder and its parents up to the configured root (or up to, but not
including, the drive for folders outside every root), each with its number of
catalog entries. The chosen folder is added to the excluded directories.

## Tray, settings and autostart

The tray menu offers Search, Refresh catalog, Settings, Start with Windows, Index
status, Clear search history, Check for updates, About, Restart and Quit. A single
click opens the launcher. Restart starts a replacement process that waits for the
running one to release its instance lock, so two keyboard hooks never run at once.

When PathScout runs from a git checkout and git is installed, it fetches in the
background shortly after start and every 30 minutes and announces new upstream
commits. **Check for updates** (or **Install update**) runs `git pull --ff-only`,
which refuses diverged or modified checkouts instead of merging, and offers a
restart after a real update. Git never prompts for credentials; a stalled git is
stopped after two minutes. Installed packages are updated with pip.

Search folders and excluded directories accept folders dragged from Explorer,
several at once; dropped files are ignored and duplicates are skipped.

Start with Windows manages only `PathScout.lnk` in the current user's Startup known
folder; the existence of PathScout's own shortcut is the state shown in both the
tray and the settings dialog. The checkbox applies immediately in either place,
because it changes a file outside the settings document. A failure shows a message
and restores the checkbox to the actual shortcut state. An unrelated shortcut with
the same name is never replaced or deleted.

All other settings apply on Save. Validation or save errors keep the dialog open
with the edits intact. Changing roots, exclusions, markers, priority rules or
application indexing restarts folder watching and rescans; changing only the result
limit, interval, backend or input settings does not rescan and keeps an Explorer scope.
The search backend selector offers Rust (default, with Python fallback) and Python.
The diagnostic environment override takes precedence over the stored choice.

User configuration (`settings.json`), catalog/history/favorites and diagnostics
live under `%USERPROFILE%\.PathScout` by default; `--data-dir` selects an isolated
directory. Existing LocalAppData settings and SQLite state are copied once, with
the originals retained. Logs rotate at 5 MB with five backups. Search timing,
shortcut dispatch, focus-owner changes and UI pauses are logged without recording
query text or window titles. Shell icon retrieval must not block the GUI thread.

PathScout starts in the tray without showing the search bar; `--show` opens it
right away, and it also opens when no system tray is available. A second start for
the same data directory shows the running launcher, unless it was started with
`--background`, in which case it exits without changing the window.
