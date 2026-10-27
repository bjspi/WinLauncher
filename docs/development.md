# Development notes

Notes for working on WinLauncher itself: its command line, how fast searches
are and how releases are built. The README covers building and testing.

## Command line

```powershell
python -m winlauncher                         # start in the tray
python -m winlauncher --show                  # start and open the search bar
python -m winlauncher --no-hooks              # no global shortcuts or Explorer typing
python -m winlauncher --data-dir D:\Temp\Try  # separate settings and catalog
python -m winlauncher --version
```

WinLauncher starts quietly in the tray; press <kbd>Ctrl</kbd> <kbd>Ctrl</kbd> to search.
Starting it again opens the running instance's search bar, unless the second start
uses `--background`.

## Local Rust build files

Debug builds omit debug symbols and incremental compiler state to keep disk use
small. Run `python tools/check.py` for the normal local checks (Ruff, both Rust
crates and both Python search engines). Its `finally` block removes both crates'
debug builds even after a failed check. After standalone Rust checks, use
`cargo clean --manifest-path native/Cargo.toml --profile dev` and the same command
with `loader/Cargo.toml`. Release builds, installed Python extensions and backup
markers stay in place. The next Rust check recompiles its dependencies.
Build output is excluded from Git and cloud backup; CI retains its build cache.

## Windows loader

The small Rust host has its own version (`1.0.0` initially). Do not bump it for
ordinary application releases. Its prebuilt Windows x64 template is committed in
`winlauncher/platform/bin`, like pip's launcher stubs, and ships in wheels and
source distributions. It works without the optional Rust search engine.

After changing `loader/`, the build script or `docs/images/icon.png`, rebuild on
Windows with Rust/MSVC and the Windows SDK installed:

```powershell
python tools/build_loader.py
python tools/build_loader.py --check
cargo fmt --manifest-path loader/Cargo.toml --check
cargo clippy --manifest-path loader/Cargo.toml --locked --all-targets -- -D warnings
cargo test --manifest-path loader/Cargo.toml --locked
python -m unittest discover -s tests -p test_loader.py -v
```

Commit the template and its JSON fingerprint with the source change. The
fingerprint covers the host sources/build instructions/icon, normalizes text line
endings, and checks the binary digest. CI checks freshness and tests Rust and the
actual host on Python 3.11–3.14; packaging reuses the template instead of recompiling
it for every app release. The binary has no third-party Rust dependencies.

Runtime setup configures a copy at `~/.local/bin/winlauncher.exe`; it records the
interpreter, DLL and package paths as Windows resources. The actual host's image
path identifies it; `sys.executable` intentionally remains Python. Tests isolate
PATH and Startup changes, exercise Unicode/quoting, venvs, Qt and Python children,
replace a running host, and cover legacy start → pending update → host → Restart.
On Windows, Qt's process pipes need a normal desktop process environment; a
restricted sandbox may reject them before process creation.

## Search performance

`tools\benchmark_search.py` times a fixed list of queries against a catalog file
and splits candidate selection into the literal and the fuzzy pass. Run it on a
copy of your catalog, since opening a catalog may upgrade its schema; it prints
only counts and times:

```powershell
python tools\benchmark_search.py COPY\catalog.sqlite3 --settings COPY\settings.json
```

Median times on a real catalog of 91,539 entries (85k files, 5.5k folders, 657
apps) with 60 results, before and after the memory index and name-only short
terms:

| Query | Before | After | Query | Before | After |
| --- | ---: | ---: | --- | ---: | ---: |
| `d` | 161 ms | 17 ms | `steuer bescheid` | 45 ms | 12 ms |
| `do` | 161 ms | 26 ms | `*.pdf` | 54 ms | 16 ms |
| `dok` | 68 ms | 27 ms | `ext:pdf rech` | 40 ms | 15 ms |
| `dokumente` | 68 ms | 27 ms | `projekt` | 40 ms | 9 ms |
| `rechnung` | 50 ms | 15 ms | `a b` | 183 ms | 32 ms |
| `rechnung 2024` | 42 ms | 10 ms | `readme` | 48 ms | 22 ms |
| `ärzt` | 36 ms | 6 ms | `xyzzyq` | 34 ms | 4 ms |
| `vsc` | 55 ms | 23 ms | | | |

The fuzzy pass used to scan the whole table on every keystroke (about 38 ms); in
memory it takes about 3 ms, and most of the remaining time is ranking and the
exclusion checks of kept hits. The memory index takes 30 MiB for this catalog
and reloads in about 100 ms after a catalog change.

### Plugin timings in the log

`winlauncher.log` in the data folder answers "which plugin makes it slow":

- `Search finished: ... plugins=jira=42.1ms/6000items/60hits,telegram=0.3ms/12items/12hits`
  — every search, each plugin's ranking time, the items it looked at and its hits;
  `jira=left-out` for a plugin over the time limit.
- `Plugin jira refreshed: items=6000 plugin_ms=2100 validate_ms=40 cache_ms=90
  prepare_ms=20 total_ms=2250` — `plugin_ms` is the plugin's own code, the rest is
  WinLauncher's work with its items. `refresh started`, `refresh cancelled` and
  `cached items loaded` frame it.
- `Plugin jira left out of ordinary searches: ...` (warning) and `... is tried in
  ordinary searches again` when its items change.
- `UI event loop delayed: 420 ms search_running=... indexing=... plugins_refreshing=jira`
  — the GUI thread stalled, and what ran meanwhile.

Measured on Windows with Python 3.12.1 and PySide6 6.11.1, using 2,170 synthetic
tickets with three keywords each, 60 results and nine repetitions per query:

| Query | 0.15.0 median | 0.15.1 median |
| --- | ---: | ---: |
| `sl` | 74 ms | 33 ms |
| `sleep` | 55 ms | 21 ms |
| `timeout` | 60 ms | 7 ms |
| `connection` | 272 ms | 60 ms |

The same mixed-query workload ran on a worker while a 2 ms Qt timer measured GUI
event delivery: its largest gap fell from 33 ms to 8.5 ms. A faster ranking alone
does not prove responsiveness: `sleep(0)` checkpoints can starve Qt's Python
callbacks, so check event delivery as well as elapsed search time. These timings
depend on the data and CPU; some queries still exceed the default 30 ms budget.
Preparing the fuzzy layouts adds about 100 ms per refresh and 15 MiB of retained
Python memory for this corpus; cache preparation runs on a background worker.

## GitHub builds and releases

CI enables `PYTHONFAULTHANDLER=1` for native crash tracebacks. To investigate a
local Windows/COM process abort, run `python -X faulthandler -m unittest discover
-s tests -v`; an isolated successful rerun does not establish the cause of a crash.

[CI](https://github.com/bjspi/WinLauncher/actions/workflows/ci.yml) runs on pushes to
`main`, pull requests and manual dispatch. It checks Python style, Rust formatting
and Clippy, runs the Windows tests with each engine, tests the Rust wheel on
Python 3.11, 3.12, 3.13 and 3.14, checks the Qt/keyboard flow, and builds and
verifies the Python wheel/source distribution and the Windows x64 Rust wheel. Download them from the run's `winlauncher-windows-packages` artifact.
Independent jobs run in parallel: the Rust wheel is compiled once, with a cache,
and every Rust test job installs that wheel. A commit message containing
`[skip ci]` pushes without starting CI.

Pushing a `v*` tag publishes a GitHub Release once the CI run of the tagged commit
passed, attaching that run's packages and a clean source ZIP; it builds and tests
nothing again. Push `main` first, so that run exists. The tag must match
`winlauncher/__init__.py`, `pyproject.toml`, `native/pyproject.toml` and
`native/Cargo.toml` (for example `v0.2.2`), and the `rust` extra in `pyproject.toml`
must point to that release's native wheel. Install both wheels from a release
with `python -m pip install <python-wheel> <native-wheel>` to enable Rust without
installing a compiler. Source ZIPs retain `native/` for local builds.

For example, publish version 0.2.2 from a clean, committed checkout with:

```powershell
git push origin main
git tag -a v0.2.2 -m "WinLauncher 0.2.2"
git push origin v0.2.2
```

The finished release appears under [Releases](https://github.com/bjspi/WinLauncher/releases).
