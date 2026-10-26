# Behavioral specification

## Catalog

The catalog records absolute path, normalized name/path, item kind, size,
modification time and Windows attribute bits. Executables and `.lnk`, `.url` and
`.appref-ms` files are applications; other ordinary files are files. Directory
entries are folders. Application display names omit their extension.

Configured roots are recursive, and each root folder is itself an entry, so `D:\Projects`
is found by name; drive roots have no name and are not listed, and an Explorer
folder outside every root only lends its scope. With application discovery enabled, Start menu
directories are scanned for launchable entries and HKCU/HKLM App Paths are read in
both registry views. Store apps, which have neither, are taken from Windows' "All apps"
list (`shell:AppsFolder`, entries whose AppUserModelID names a package): before each
full scan WinLauncher keeps one shortcut per Store app in
`%LOCALAPPDATA%\WinLauncher\Store apps` and indexes that folder like a Start menu
folder. Shortcuts show the icon they name or their target's, without the shortcut
arrow.

With Windows tools and settings enabled (the default), each full scan keeps
shortcuts in `%LOCALAPPDATA%\WinLauncher\Windows settings`, indexed the same way,
one subfolder per kind. All names come from Windows in the display language, so
nothing is translated by WinLauncher:

- `Control Panel`: all Control Panel items and the tasks of "All Tasks" (God Mode,
  `shell:::{ED7BA470-8E54-465E-825C-99712043E01C}`), as shortcuts to their shell
  items, with their own icons.
- `Settings`: the entries of the Settings app's search index
  (`%WINDIR%\ImmersiveControlPanel\Settings\AllSystemSettings_*.xml`), titled with
  their resolved description and opening their page with `explorer.exe ms-settings:…`.
  The page-to-link table is read from `SystemSettings.dll`, where each page id
  precedes its `ms-settings:` link. Entries whose page has no link, and titles longer
  than 90 characters, are left out.
- `Tools`: the management consoles in `System32\*.msc`, titled from the console in
  the display language (`System32\<language>\*.msc`), and a fixed list of system
  programs titled with their file description. Both keep their file name in the
  title, as in *Dienste (services.msc)* or *Task-Manager (taskmgr)*.

*Settings detail* chooses between every setting (the default) and pages only.
Pages only keeps the Control Panel items without the God Mode tasks, and only the
search-index entries Windows names after their page (`AAA_<page id>`, variants
`AAA_<page id>-<n>`), such as *Standard-Apps* or *Soundeinstellungen*.

The option *Also in English* repeats this below `English` with the thread's display
language set to `en-US`, leaving out names the display language already has. It
can be enabled only for a Windows shown in another language; turning it off
removes the `English` folder.

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
refresh interval is 1,800 seconds; it only repairs events the folder watcher missed. The interval is configurable from 30 to 86,400
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

Paths longer than Windows' 260-character limit are indexed, watched and searched
like others: file-system calls use the extended-length form (`\\?\C:\...`,
`\\?\UNC\server\share\...`) from 240 characters on, while the catalog, the UI
and opened paths keep the ordinary form. This does not depend on the system-wide
long-path setting.

## Exclusions

An explicit directory exclusion applies to itself and every descendant, with path
component boundaries (`cache` does not exclude `cache-copy`). Matching uses absolute,
nonexpanding uppercase path identities. Search text independently uses NFC, full
case folding and no accents (the combining marks of the canonical decomposition). Alternate Windows casing retains favorites and usage; Unicode names
that merely share search normalization remain separate identities. Case-sensitive
NTFS directory namespaces are not supported.

Patterns without a separator match file and directory names; `*`, `?` and bracket patterns
use Python fnmatch semantics. Patterns with `/` or `\` match folder paths; both
separators are equivalent and matching is case-insensitive. In them `*`, `?` and
`[…]` stay within one folder name, while a `**` segment spans any number of
folders (including none). A pattern starting with a drive (`D:/Tools/*`) or a
separator (`\\server\share\*`) matches from the root; any other path pattern
matches the trailing folders of a path, so `Projects\Android\*\build` excludes
`D:\Work\Projects\Android\App\build` and `Android/**/build` excludes every `build`
folder at any depth below an `Android` folder. Matching directories exclude their subtree.
For example, `*.lnk` excludes Windows shortcuts by default. Name patterns without
a separator do not apply to entries from application sources (Start-menu folders and
registered App Paths), so installed applications stay searchable; folder exclusions,
path patterns and markers still apply to them. Filename/extension
priority rules with `search_priority: "ignored"` can also exclude files by name or type.

A configured marker name found in any ancestor directory excludes that directory
and its descendants, including when the ancestor is above a scan root. A marker
is detected by existence; WinLauncher does not modify it. Defaults are `.nobackup`,
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
is ignored; apostrophes are literal characters. A search term written with double
quotes is exact: it matches only as contiguous text, never by initials or as a fuzzy
subsequence; quoted filter values and exclusions are unaffected. Repeated `ext:`,
`path:` and folder filters accumulate; size bounds intersect, and so do all date
bounds (`date:`, `after:`, `before:`); for other filters the last value wins.
Unsupported dates return a validation error. Windows backslashes remain literal.
Matching is NFC-normalized, case-insensitive and accent-insensitive in both directions:
`lohn` and `löhn` both find `Lohn` and `Löhne`. A term the path contains only with other
accents than typed ranks lower: by 5 points when the path has accents the term lacks
(`lohn` lists `Lohn` before `Löhne`), and by 45, about one kind of match, when typed
accents are missing (`löhn` lists `Löhne` before `Lohn`). Fuzzy matches, folder parts,
extensions and exclusions ignore accents.

| Expression | Contract |
| --- | --- |
| `annual report` | Every term must match the name or literal full path; one- and two-character terms match the name only |
| `"annual report"` | One exact term containing a space: contiguous text only, no initials or fuzzy subsequence |
| `*.json`, `report?.json`, `report[0-9].json` | Whole-filename fnmatch patterns, including the extension |
| `type:file`, `type:folder`, `type:dir`, `type:app` | Restrict item kind; overrides the UI kind button. `t:` is shorthand for `type:` |
| `ext:pdf,txt`, `ext:pdf;txt`, `ext:(jpg,png)`, `ext:doc*` | Any listed extension/fnmatch pattern, case-insensitive, optional leading dots and list parentheses; `,` and `;` separate |
| `in:"D:\Work Projects"` | Restrict to descendants of this absolute folder |
| `D:\`, `D:\Work\`, `"D:\Work Projects\"` | An absolute folder ending with a separator acts as `in:` |
| `path:projects/reports` | Match parent folder components in that order; intervening components allowed |
| `reports\ 2024`, `work\client\ proposal` | A relative token ending with `\` or `/` acts as `path:` |
| `name:annual` | Match the stem only, excluding the extension and parent path |
| `!archive`, `-archive`, `-"old version"` | Reject a literal substring anywhere in the full path |
| `!ext:tmp,bak`, `-ext:(tmp;bak)`, `!*.bak` | Reject these extensions/fnmatch patterns; `*.ext` only when `ext` has no wildcard, dot or separator |
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
| `date:today`, `date:yesterday` | Since local midnight today; yesterday's whole local day |
| `date:week`, `date:month`, `date:year` | Since local midnight on the first of the last 7, 30 or 365 calendar days, today included |
| `date:2026`, `date:2026-07`, `date:2026-07-15` | That whole local year, month or day |
| `date:2026-01..2026-06` | From the start of the first period through the end of the last, inclusive |
| `date:>2026-07`, `date:>=2026-07` | After the end of the period; from its start |
| `date:<2026-07`, `date:<=2026-07` | Before the start of the period; through its end |
| `size:>5kb`, `size:<2mb` | Strict file/application size comparison; `>=` and `<=` include the boundary |
| `size:10mb..1gb` | Inclusive file/application size range |
| `contains:"invoice number"`, `contains:gsk_*` | Case-insensitive text/fnmatch content filter; needs the Rust engine |
| `content:text`, `rg:text` | Aliases for `contains:`, including its fnmatch semantics |
| `low:true` | Include low results even when ordinary/high results exist |

An Explorer session supplies the default folder scope. An explicit `in:` or an
absolute folder token ending with a separator overrides it; such a token expands
variables like `in:`. The scope directory itself is excluded from its descendant
results. Folder-path matching uses the selected keyword/fuzzy matcher on each
parent component. Unknown prefixes remain literal text, which allows ordinary
paths containing colons; a path such as `C:\Work` without a trailing separator
stays search text, and a token of separators alone is a term.

A token starting with `!` or `-` and longer than that character is an exclusion,
including quoted text (`-"old version"`). A lone `!` or `-`, and a dash inside a
word (`my-file`), is ordinary search text. `!ext:`/`-ext:` accept the same lists and
patterns as `ext:` and reject candidates of any kind whose extension matches;
exclusion wins over an including `ext:`. Other exclusions, including `!*.tar.gz`,
`!*.b?k` and `!type:file`, remain literal path substrings.

Size units use powers of 1024, case-insensitively: bytes (`b`, `byte`, `bytes`),
`kb`/`kbyte`/`kbytes`/`KiB`, and equivalent `m`, `g` and `t` prefixes. No unit means
bytes. Decimal points and commas are accepted; byte bounds are rounded exactly
according to the comparison. Spaces after `size:` or its comparison are allowed.
A range `a..b` rounds its start up and its end down to whole bytes, and both ends
need an amount; `size:1gb` without a comparison or range is an error. Repeated
comparisons and ranges form an intersection; invalid units, out-of-range byte bounds
and empty ranges report validation errors. Folders are excluded by size filters;
no recursive folder-size calculation is performed. SQL, Python and native ranking
apply the same inclusive integer byte bounds. An extension with a different API
version is not used, so newly added filters cannot be silently ignored.

Dates accept `YYYY`, `YYYY-MM` or `YYYY-MM-DD`, with an optional trailing `*` or
`-`. Missing parts expand to the first day for `after:` and the last day for
`before:`; leap years and local midnight boundaries are respected. Any other day
that Python's `date.fromisoformat` accepts is also valid, such as the compact
`20260215` and ISO week dates like `2026-W07` (its Monday) or `2026-W07-3`.
`date:` periods, ranges and comparisons use the same forms; any single ISO day is a
one-day period. Relative names (`today`, `yesterday`, `week`, `month`, `year`) are
case-insensitive, belong to `date:` only and are resolved against the local date
when the query is parsed; they cannot be combined with `..` or a comparison.
Relative values except `yesterday` have no upper bound. A range ending before it
starts, or intersecting date filters without a common day, is an empty-range error.

Fnmatch patterns support `*`, `?`, character sets/ranges and `[!...]` negation;
`[[]` escapes an opening bracket and an unmatched `[` is literal. `*` has no
special recursive meaning. Quoting preserves patterns. Matching uses the same
normalization as ordinary terms, with either slash spelling accepted in paths.
Without a separator, patterns match only the display name or actual filename,
never a parent folder. `name:` matches the stem; `path:` matches ordered parent
components. Patterns are not fuzzy, but accompanying ordinary terms may be.
The Python engine uses standard-library fnmatch; Rust uses its own translator
(`native/src/glob.rs`), which mirrors CPython's `fnmatch.translate`, with regexes
compiled once per query. SQL only supplies a safe candidate superset: excluded
extension patterns are left to the engines, and exact terms use substring rather
than subsequence patterns in the fuzzy pass. Excluded terms remain literal
substrings.

Rows show approximate catalog size when a size filter is active and the local
modification timestamp when a date filter is active, including in Explorer mode
and accessible text. Painting performs no filesystem metadata reads. Creation
timestamps are not stored or used by the modification date filters.

Content search uses ripgrep's `grep-regex` and `grep-searcher` crates, with one
fnmatch matcher compiled per query and a reusable streaming buffer per worker. It applies
after ordinary ranking and exclusion checks, before a candidate enters the result
heap. Candidates beyond the result limit remain eligible, so an early set of
content mismatches cannot hide later matches. Contents are read afresh without a
content index. Only allowlisted text/source/configuration extensions and common
extensionless text names are eligible. Files must be regular, locally available
and at most 64 MiB according to current filesystem metadata. BOM-marked UTF-16 is
decoded automatically; NUL detection skips binary input. Office/PDF/media and
executables are not parsed. Unreadable, deleted or unsupported files do not match.
Read chunks check cancellation, and scanning runs with the Python GIL released.
`content_search_threads` selects 1–16 parallel file scans (default 4), exposed in
the Search folders settings page. It changes no catalog membership and needs no rescan.
Only candidates that pass ranking and exclusions enter a bounded queue (at most
twice the worker count in flight). Results enter the heap in candidate order,
preserving ranking, ties and result limits. Workers are joined when the search
finishes or is cancelled; ordinary filename searches start no content workers.
Each worker uses the ripgrep crates directly, without launching a process.
Increasing parallelism can increase disk activity and buffer memory use.
`contains:` requires nonblank text of at most 128 characters. Fnmatch patterns
(`*`, `?`, `[0-9]`, `[!0-9]`) match substrings within individual lines, without
spanning line boundaries. Regex punctuation remains literal; `[*]`, `[?]` and
`[[]` escape wildcard characters. Repeated filters keep the last value. Python mode
or an incompatible extension reports a clear error; it never silently drops the
content constraint.

`contains:`, `content:` and `rg:` share validation, matching and the canonical
`contains:` history key. Mixed repeated aliases keep the last value. They all use
fnmatch syntax, with identical behavior for regex punctuation.

Extension lists are OR alternatives, also across repeated `ext:` filters.
Optional parentheses may contain spaces, for example `ext:(jpg, png)`. Each
extension/pattern is normalized without its leading dot and matched in full;
`ext:doc*` includes `doc`, `docx` and `docm`, while `ext:doc,docx` includes only the
two named extensions. SQL narrows wildcard candidates conservatively, and both
engines apply the complete fnmatch pattern before retaining a result.

The footer exposes Files, Folders and Apps toggles, also reachable with Alt+1,
Alt+2 and Alt+3. Clicking the active toggle again or using Alt+0 restores All.
Typed kind filters update toggle/menu state. Selecting a toggle removes typed
`type:`/`t:` overrides while preserving other query tokens and quoted values.

Leading `%VARIABLE%` and `~`/`~/`/`~\` enter direct path navigation. An exact
existing path returns the file/folder; a trailing separator lists its direct
children, and an incomplete last component completes names by prefix. `%` and an
incomplete name suggest every `%NAME%` that starts with the typed text and names an
existing folder: environment variables whose value is a folder (so values such as
tokens never appear) and Windows known folders without a variable, such as
`%DESKTOP%`, `%DOWNLOADS%`, `%STARTUP%`, `%SENDTO%`, `%FONTS%` or `%SAVEDGAMES%`.
The platform layer resolves known folders through Windows, so redirected folders
are followed; names reaching the same folder appear once. Environment variables win
over known-folder names. Unknown completed names and relative expansion values
report an error. Resolution and enumeration run on a search worker, obey
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

These weights are WinLauncher's own, chosen and tested independently.
For each positive term, the first matching rule supplies its text score:

| Match | Score |
| --- | --- |
| Exact name or stem | 150 |
| Name prefix | 110 |
| Word prefix | 90 |
| Name substring | `75 - 0.05 × starting offset` |
| Initials / camel-case abbreviation | 65 |
| Literal full-path substring (terms of three or more characters) | 35 |
| Fuzzy name subsequence | 5–60 |

Plain terms shorter than three characters never match the parent path, in SQL
candidate selection, eligibility and scoring alike: nearly every path contains a
drive letter or some other one- or two-letter fragment, so `d` would otherwise
make the whole catalog a candidate through `D:\`. Wildcard patterns, `path:` and
folder filters keep their own rules.

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
learned set. This start list is computed ahead in a separate prefetch pool while
the bar is hidden (after it closes and after catalog or personal changes), so the
bar opens with it at once; a fresh search then confirms it without the typing delay. An explicit constraint such as `ext:pdf` can browse unlearned entries
without positive text. `score:match` removes all those bonuses.
Ties use case-insensitive display name then absolute path. A favorite or explicit
boost can outrank a stronger text match in combined mode. Score explanations are
available in tooltips and expanded view.

Only a successful shell open records a launch. The learning key consists of
normalized positive terms, effective item kind, sorted extension filters and ordered
folder components. Scope, exclusions, score/match mode, size and date filters are
omitted, so related searches share history and a relative `date:today` keeps its key
from day to day. Quoted and unquoted spellings of a term share it too. History clearing preserves favorites. No query
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

Quick Switch (setting `quick_switch`, on by default) extends Explorer typing to the
common Open and Save dialogs: a `#32770` dialog that hosts a `SHELLDLL_DefView`
file list. Typing starts only while that file list has the focus, that is after
the user clicked into it. The file-name box (where Open dialogs start), the address
bar, search box and navigation pane keep their keys, so names typed there are left
to the dialog. WinLauncher's own file and folder pickers in Settings work the same:
they are modal only to the Settings window, never to the whole application, because
Qt would otherwise block the search bar's keyboard input while they are open.
Escape in the dialog's bar returns the typed text to the file-name box with the
caret at its end, so a name or path meant for the dialog is not lost. `Shell.Application` does not list dialogs, so the folder is read from the
dialog's breadcrumb toolbar, whose text is a localized label followed by the path
(`Address: C:\…`, `Adresse: C:\…`). The path is recognized by its drive letter or
UNC prefix and must be an existing directory. Places without a path, such as
Desktop, This PC or Libraries, still open the bar, because it searches everywhere;
navigating from them works the same way. The same compact bar opens, docked to the dialog, without
arrow-key selection in the dialog. Unlike Explorer typing it searches the whole
catalog rather than the dialog's folder, so any folder, such as the desktop, is a
few keys away. Choosing a folder enters its path into the
dialog's file-name box (control 1148 in Open dialogs, otherwise the first visible
edit outside the address bar and file list), sends the default button's command as
Enter would, waits up to three seconds for the address bar to show the folder and
then restores the file name the user had typed. Choosing a file does the same for
its folder and then enters the file's name and focuses the box without confirming.
Ctrl+G with a file dialog in front, and no other modifier, navigates the dialog to
the current folder of the most recently focused Explorer tab, keeping the typed
file name; it is swallowed only then and passes through everywhere else, also when
that tab shows a virtual folder or has closed. Only window messages that Windows
marshals between processes are used; nothing is injected into the dialog's process.

Open uses the Windows file association or shortcut; reveal selects the result in
Explorer. Applications receive their containing directory as the working directory.
Clipboard actions copy path, filename or parent path. Enter during an outstanding
query waits for matching results rather than opening an older result; a new query
selects its top result. These actions accept
filesystem entries rather than shell commands; WinLauncher does not implement a
command interpreter. Excluding a result's directory asks for confirmation showing
the exact directory, updates settings and refreshes the catalog. Unknown advanced-rule
JSON keys are rejected instead of silently dropping the user's rules.

## Launcher

The launcher is one compact bar: the query field, an optional folder-scope chip and
the application icon, which opens a menu with the type filters (All, Apps, Files,
Folders), Refresh catalog, Expanded view, Settings, About, Restart and Hide. The bar grows with up
to nine results (fourteen in expanded view) and shrinks back to the bare query
field for an empty query without history. Rows show the shell icon (one per file
extension, an application's own, the standard folder icon, and their own icon for
the user's known folders such as Desktop and Downloads), the name with
matched characters highlighted, the containing folder and a `Ctrl+1`…`Ctrl+9`
shortcut that opens that row. Messages (no matches, invalid query, failures),
the result count and persistent problems appear below the results only when
present. The bar can be dragged by its empty areas.

Typing in Explorer opens a smaller one-line variant docked to the bottom right of
that Explorer window, over the date and type columns rather than the names; it
grows upwards and shows each result's subfolder relative to the searched folder.
When the best result is a direct entry, Explorer selects it and scrolls it into
view without taking the keyboard focus, replacing whatever Explorer's own
type-ahead picked. Moving through the results with the arrow keys selects the same
entry in Explorer, or the subfolder that contains a deeper match. The bar closes as
soon as another program comes to the foreground: WinLauncher watches foreground
changes through Windows itself, because Qt misses the activation of a window that
WinLauncher forced to the front. Explorer completion shows matching direct folders and files
first, then matches from subfolders below a separator line; subfolder matches
always rank below every direct entry. A regular `in:` query lists direct
entries before deeper matches; relevance orders each group, and priority classes
still come first.

When the query is a calculation (`winlauncher/calculator.py`), the launcher shows
its result in a row above the results and selects it: Enter or a click copies the
result, Down moves to the first result and Up from there returns to the
calculation. The search still runs. Explorer typing has no calculator. A query
counts as a calculation only with an operator, `%`, a function call or a
`0x`/`0b`/`0o` literal and only if it parses completely; lone numbers and
constants and digits joined by hyphens remain searches. `+` and `-` apply a bare
percentage to their left operand (`200 + 10%` is 220); elsewhere `x%` is x/100.
`^` and `**` are right-associative powers that bind tighter than unary minus
(`-2^2` is -4). Decimal commas are accepted outside argument lists and inside
them when `;` separates the arguments; the result then uses a decimal comma.
Results show up to 12 significant digits; integer powers beyond about 1000
digits, non-finite and complex results show nothing. The parser never evaluates
Python code.

The result context menu also offers **More options…**, Windows' own classic
context menu for the file or folder, with the entries other programs add (Open
with, Send to, 7-Zip and so on). Shift+right-click opens it directly, including
the extended entries Explorer shows with Shift. A hidden owner window passes menu
messages to the shell's IContextMenu3 so lazily filled submenus work; the bar
closes after a command runs.

Below Open and Show in containing folder, the menu lists the user's own commands
from Settings → Context menu: a title, a single-line command line and whether it
shows for folders, files or both; applications get none. `{path}` is the result,
`{folder}` the result if it is a folder and otherwise its folder, `{name}` its
name. `%VARIABLE%` references take the environment's value and stay as typed when
undefined. Variables expand before the placeholders, and placeholders are replaced
in one pass, so a path containing `%` or `{name}` is inserted unchanged. A drive or
share root gains a trailing `.` (`D:\.`), so `"{folder}"` never ends in `\"`, which
programs read as an escaped quote. The command line goes to CreateProcess as typed,
without a shell, with `{folder}` as the working directory and a new console for
console programs; the bar closes first, and a program that cannot start is
reported. Defaults: **Open in Windows Terminal** (`wt.exe -d "{folder}"`) and
**Open Command Prompt here** (`cmd.exe /k pushd "{folder}"`, like Explorer's own
entry; `pushd` also maps network folders), both for folders.

Each command shows an icon: its chosen `.ico`, `.png`, `.svg`, `.webp`, `.exe` or `.dll` file
(`%VARIABLES%` expand), or else the first icon of the program its command line
starts, the quoted or first word found on the PATH. An app execution alias such as
`WindowsApps\wt.exe` is an empty file, so its icon is read from the packaged
program its reparse data names. Icons load when the menu first shows them and are
kept until the commands change; a command without one shows none. In a menu with
icons, every title starts in the same column. **Duplicate** in the settings copies
the selected command below it, for variants such as Claude Code and Codex.
**Remove selected** asks for confirmation first.

A command may have a shortcut modifier: Alt, Shift or Alt+Shift (stored as
`"shortcut"`, empty for none). Modifier+Enter runs it on the selected result,
modifier+click on the clicked result (the second click of a double-click does
nothing more) and Ctrl+modifier+1…9 on that numbered result, instead of opening
it. Only these modifiers are offered: Ctrl+Enter and Ctrl+1…9 are taken, Ctrl+Alt
is AltGr on many layouts and Windows reserves Win. The digit is read from the
virtual key, so Ctrl+Shift+1 (typed `!`) and Ctrl+Alt+2 (AltGr: `²`) still name
results 1 and 2. A modifier no command uses keeps its old meaning. If commands use
it but none for this result's kind (a file, an app, a website), the bar says so
and nothing runs. Saving settings refuses two commands with the same shortcut for
folders or for files; **Duplicate** leaves the copy without one. The menu shows a
command's keys beside its title, such as `Alt+Enter`.

The result context menu offers **Exclude this or a parent folder…**. A picker lists
the result itself and its parents up to the configured root (or up to, but not
including, the drive for folders outside every root), each with its number of
catalog entries. The chosen path is added to the excluded directories; a file
there excludes exactly that entry, applications and Windows tools included. For a
shortcut WinLauncher generates (Store apps, Windows tools and settings) the picker
offers only the entry itself, since its folders are switched off in Settings.
**Custom rule…** instead opens the chosen folder's path for editing, for example
into `**\patches\src`; the edited text, trimmed and unless empty, is added to the
excluded patterns.

## Search plugins

Plugins are loaded once at start, and WinLauncher ships none. A plugin is a folder
`<name>/plugin.py` with an optional `<name>/plugin.toml` whose `version` is shown
in its settings card; a loose `<name>.py` file is a plugin too. Packages declaring
entry points of the group `winlauncher.plugins` load first, then every plugin of
`<data>/plugins` (installed from a source or copied in by hand), then each plugin
folder activated in a local source (`active_plugins`), from where it lies; nothing
else is loaded. Every `Plugin` subclass a module defines itself is one plugin. The
first plugin claiming an id wins. A module that fails to import, an id that is not
`[a-z0-9][a-z0-9_-]*`, an id already taken, an unreadable folder or manifest, a
folder with a manifest but no `plugin.py` and an activated folder that is gone are
skipped and named on the Plugins page; a folder that is neither is
ignored silently and an entry whose name starts with `_` is never loaded. A loaded
plugin without a settings entry is on.

The loader also rejects, with a message naming the mistake, a plugin that does not
implement `refresh`, has no label, or declares a description, icon, keyword,
keywords (or both of these), routes (ids `[a-z0-9][a-z0-9_-]*`, unique, with a
label), `round_icons`, `refresh_minutes` or `options` that WinLauncher could not use — the
settings page and every result row read these, so they are checked once instead of
failing later. A `PluginItem` whose key or name is missing or whose other fields
are not text fails its refresh. Settings buttons a plugin declares that are not
usable `PluginCommand` values are dropped. No plugin call runs on the GUI thread,
and the worker threads are daemons, so a plugin stuck in a call delays only other
plugin work and never prevents WinLauncher from quitting.

Settings has two plugin pages. **Plugins** gets plugins: what the sources offer,
the problems of the last start, the source list and the plugin folder. **Plugin
settings** holds one card per loaded plugin, all on one page. A card's header shows
the plugin's icon (the manifest's `icon`, else the plugin's own, else a plug), its
name and description, a chevron and the on switch. The other rows are folded at
first; the chevron or a click on the header unfolds them, and which cards are
unfolded is remembered at once in `<data>/plugin-cards.json`, apart from Save. A
plugin that is off shows its header only.

Settings store per plugin `id`, `enabled`, `keywords`, `routes` (route id to its
keywords), `in_search`, `refresh_minutes` (1–1440), `boost` (-100–100) and
`options` (names to booleans, integers or strings; an invalid or missing value uses
the plugin's default). A plugin without an entry uses the keywords and route
keywords it suggests; an entry of 0.13 or older with one `keyword` reads as that
keyword. Keywords are letters, digits, `_` and `-`. Every keyword and route keyword
of the enabled plugins must differ from website keywords and from each other,
ignoring case and accents; a route the plugin no longer declares claims no word.

An enabled plugin refreshes 4 s after start (or after it is enabled) if its cached
list is older than its interval or the last refresh failed, then at its interval.
Its last list stays searchable during a refresh and after a failed one. A plugin
that needs setup loses its list. **Refresh catalog** in the tray or the bar's menu also refreshes every enabled
plugin; each plugin's settings card has **Refresh now**. A plugin lists at most 5,000 items; later items
and repeated keys are dropped.

Typed text that starts with an enabled plugin's keyword lists that plugin's items
first, searched for the words behind the keyword; without any they come in the
plugin's order. All keywords of a plugin work alike. A route's keyword does the
same with only the items whose `routes` name that route. The same text still searches the catalog, so a short keyword never
takes its word away: `tg` lists the chats and finds a file named `tg`. The prefix
`@` asks for the plugin alone and leaves the catalog out, as in `@tg anna`; an `@`
before anything else is ordinary search text. Plain word searches without filters,
sorting or scope add the items of every enabled plugin with **In every search**
on; website keywords and file filters do not. An item scores the catalog's text match of each term
against its name, or against a keyword 10 points lower. It adds
`12 / (1 + position / 10)` for its position in the plugin's list and the plugin's
boost. Every term must match, and an excluded term in the name or keywords drops
the item. Plugin hits join the file hits by score and the result limit applies to
both.

Plugin items are ranked in Python on the search worker, and Python runs one thread
at a time, so a big plugin makes typing lag even though it searches in the
background. Ranking therefore yields to the GUI thread every 256 items and stops
there when the search was typed over, keeping no plugin hits. Each plugin's
ranking is timed; a plugin that takes longer than **Time limit per search**
(`plugin_time_limit_ms`, 0–1000 ms, default 30, 0 turns it off) in 3 ordinary
searches in a row is left out of ordinary searches until its items change, such as
at its next refresh. Its keywords still search it. Its settings card shows the last
search time and whether it is left out.

A term that contains a digit names something — a ticket, a version, a phone
number — and is matched in one piece even in fuzzy mode: `12345` finds the ticket
12345 but not 52314, while the words of the same query keep matching loosely, so
`12345 login` and `uC halted` both work as expected. A plugin's `literal_terms`
are case-insensitive patterns naming further whole terms to match that way, such
as a ticket prefix without digits; the loader rejects a plugin whose patterns do
not compile. Quoted terms and `match:keyword` are literal as always.

A row shows "Label: name" (or the item's own title), the subtitle as its
location and the item's image. A plugin with `round_icons` crops images to
circles and paints colored initials for items without one. Enter, a click or
Ctrl+1…9 hides the bar and runs the plugin's `open` on the action worker. The
default opens the item's target with `os.startfile`, and a failure is shown
like other failed actions. The context menu offers Open in <label> and Copy
Link; file actions and own menu commands do not apply.

Plugin files — `plugin.py`, `plugin.toml`, `catalog.toml` — are read as UTF-8,
with or without a byte order mark, and otherwise as Windows-1252; a coding
declaration in `plugin.py` comes first. Output of git and pip is decoded the same
way, and pip and pipx run with `PYTHONIOENCODING=utf-8`.

### Plugin sources

Settings store `plugin_sources`, one entry per source; the default is
WinLauncher-Plugins. An absolute drive or share path is a **local** source, read
in place; anything else is a **remote** git repository (https, SSH or `file:`
address). Entries that differ only in letter case, a trailing slash or `.git` are
the same source and may not be listed twice. A source offers plugin folders in one
of three layouts: a `catalog.toml` at its root naming the source (`name`, else its
address is shown) and the folder of its plugins (`plugins`, default `plugins`,
which must lie inside the source); the source being one plugin folder; or a folder
of plugin folders. Folders starting with `_` or without `plugin.py` are not
offered. Settings written by 0.12 or 0.13 are read as sources: their catalogs and
plugin folders become sources, and every plugin those folders loaded is activated.

**Available plugins** on the Plugins page lists every offer with its manifest's
`name` (else the folder name), `version`, `description`, `requires` and `icon` (an
image file inside the plugin folder; else a generic plug), and its state. A remote plugin is not installed, installed, has an update (the installed
version differs), or is blocked because `<data>/plugins` holds a folder of that
name that did not come from this source; a local one is active or not. A state that
changes at the next start says so. A plugin with `requires` shows the command that
installs those packages into WinLauncher's environment (`pipx inject` for a pipx
installation, else `python -m pip install` on WinLauncher's interpreter).

- **Install and activate** and **Update** (remote) ask for confirmation, install the packages,
  then copy the folder into `<data>/plugins` with an `installed-from.txt` naming
  the source, replacing an older copy in one rename. When the packages fail,
  nothing is copied and the message names the command to run.
- **Uninstall** (remote) asks, then deletes the folder; settings and data stay.
- **Activate** (local) asks, installs the packages, then adds the folder to
  `active_plugins`; **Deactivate** removes it. Both are edits of the settings,
  saved with the others. An activated folder whose source is no longer listed is
  dropped on save.

Installed and activated plugins are on from the next start (their settings card
is switched on if it was loaded before). After a change the page offers **Restart
now**, which saves the settings first.

Sources are read at start, then checked 60 s later, every 6 hours, on **Check
now** and when a saved edit changes the list; git runs without a console and never
prompts, giving up after 120 s, and package installations give up after 600 s. A
remote source that cannot be checked keeps listing what its last clone offered,
with the reason; an unreadable source, `catalog.toml` or manifest is named on the
page. Clones of sources no longer listed are deleted. A new version of an
installed plugin is announced once in a tray message.

## Tray, settings and autostart

The tray menu offers Search, Refresh catalog, Settings, Start with Windows, Index
status, Clear search history, Check for updates, About, Restart and Quit. A single
click opens the launcher. Restart starts a replacement process that waits for the
running one to release its instance lock, so two keyboard hooks never run at once. Clear search
history asks for confirmation first and keeps favorites.

Updates follow release tags (`vX.Y.Z`), never unreleased commits, because only a
release ships the matching prebuilt Rust engine. When git is installed, WinLauncher
asks the remote for its tags (`git ls-remote`) shortly after start and every 30
minutes and announces a release newer than its own version. A git checkout asks
its `origin`; an installed package asks the project repository. Pushing a tag
starts the workflow that tests and only then publishes the release, so a newer tag
is announced only once its Rust engine wheel exists: a HEAD request for the wheel,
without following GitHub's redirect to the download host, answers with a redirect
for a published release and 404 before. A 404 is asked again after 5 minutes;
an unreachable GitHub waits for the next regular check. A checkout then fetches
just that tag before announcing it; a failed fetch is retried with the next check.
A known release turns the tray entry into **Update to X.Y.Z…** and shows a
notification naming the version.

In a checkout, **Check for updates** (or **Update to X.Y.Z…**) announces
"WinLauncher X.Y.Z is available" with the running version; **Show changes** expands the
release's commits, changed files and diff (cut off after 200,000 characters),
read from the fetched tag. **Update** fetches the tag again and runs
`git merge --ff-only` to it, which refuses diverged or modified checkouts instead
of merging and leaves a checkout already past the release where it is. A refused
update says it is usually caused by locally changed files, keeps git's output
behind **Details** and offers **Force update…**: after a confirmation it runs
`git stash push --include-untracked` and `git reset --hard` to the tag, so local
changes stay in the stash, ignored files and the settings in the data directory
are untouched, and the output starts with the previous HEAD so replaced commits
can be found. After a real update, forced or not, it marks a reinstall as pending
and offers a restart;
the next start runs `pip install -e ".[rust]"` before anything loads the old
engine, whose `.pyd` Windows locks while it runs. A failed reinstall, for example
offline, stays pending: the next starts try again, three in all, and the message
shows the command to finish it by hand. For pipx and other installed
packages, WinLauncher shows the exact install command to copy instead of replacing
its own running environment. Git never prompts for credentials; a stalled git is
stopped after two minutes.

Search folders and excluded directories accept folders dragged from Explorer,
several at once; dropped files are ignored and duplicates are skipped.

Explorer typing uses a 396-pixel-wide surface with 25-pixel one-line rows, 16-pixel
icons and up to six visible results. The search enumerates only the current
directory on its background worker and matches entry names, not the common parent
path. Direct folder names participate even when index exclusions or marker files
exclude their contents; files still obey exclusions. Folders precede files before
the result limit. Subfolder matches come from the catalog,
with its normal exclusions, and leave out the catalog's copies of direct entries;
they match the path below the searched
folder (so `invoices 2024` finds files inside such a subfolder, while the searched
folder's own name matches nothing). The live listing writes nothing to the catalog.
Content filters keep their Rust requirement and use the same bounded content workers.
The captured Explorer window and active-tab handles travel with the keyboard
intent. A single click or Enter on a folder uses that tab's `Navigate2` method;
stale, changed or ambiguous targets fail instead of opening another window.
File opening and global launcher actions retain their normal shell behavior.
Clearing the scope restores the global launcher and drops the Explorer target.

Start with Windows manages only `WinLauncher.lnk` in the current user's Startup known
folder; the existence of WinLauncher's own shortcut is the state shown in both the
tray and the settings dialog. The checkbox applies immediately in either place,
because it changes a file outside the settings document. A failure shows a message
and restores the checkbox to the actual shortcut state. An unrelated shortcut with
the same name is never replaced or deleted.

Settings is a window of its own, not modal and without an owner, so the search bar
can come in front of it and stays usable, for example to try a new context-menu
command. Opening Settings, or again, which brings the open window forward, hides the
bar: the bar stays on top of every window, so whatever opened last is in front, and
the bar returns over Settings when called again. An exclusion added
from a result's menu meanwhile is saved at once and also joins the open window's
fields, so its next Save keeps it.

All other settings apply on Save. Validation or save errors keep the dialog open
with the edits intact.

Messages and questions use WinLauncher's own dialog instead of a stock message
box: an icon badge tinted by kind (information, question, update, success,
warning, error), a heading with the text below it, optional details behind a
link that names what a click does, and buttons that name their action. Enter
runs the filled main button; a destructive question (red button) has none, so
Enter cancels. About, Index status and the update dialogs share this look. Changing roots, exclusions, markers, priority rules or
application indexing restarts folder watching and rescans; changing only the result
limit, interval or input settings does not rescan and keeps an Explorer scope.
Input settings (double-Ctrl interval, Explorer typing, Quick Switch) restart the
keyboard listener.

WinLauncher searches with the Rust engine. When the extension is missing or does
not support the running Python, it searches with the Python engine, shows the
reason in the search bar and rejects `contains:` with that reason. An unexpected
Rust error is logged once per kind, and that search is repeated in Python.
`WINLAUNCHER_SEARCH_ENGINE=python` forces the Python engine; `native` requires the
Rust engine and reports its errors instead of falling back.

The Rust engine selects candidates from an in-memory copy of the catalog's
entries instead of scanning the table with `LIKE` on every keystroke; it selects
exactly the rows of the candidate SQL. A search never uses a copy older than the catalog snapshot it reads:
every change to an entry increments the catalog version, and a search that sees
a newer version reloads the copy first, or uses candidate SQL while another
search is reloading it. Launch history and favorites are read fresh on every
search. `WINLAUNCHER_NATIVE_PHASE=sqlite` (candidate SQL in Rust) and `rows`
(candidate SQL in Python) select the earlier candidate sources for diagnosis.

User configuration (`settings.json`), catalog/history/favorites and diagnostics
live under `%USERPROFILE%\.WinLauncher` by default; `--data-dir` selects an isolated
directory. Existing LocalAppData settings and SQLite state are copied once, with
the originals retained. Logs rotate at 5 MB with five backups. Search timing,
shortcut dispatch, focus-owner changes and UI pauses are logged without recording
query text or window titles. Shell icon retrieval must not block the GUI thread.

WinLauncher starts in the tray without showing the search bar; `--show` opens it
right away, and it also opens when no system tray is available. A second start for
the same data directory shows the running launcher, unless it was started with
`--background`, in which case it exits without changing the window.
