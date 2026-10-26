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

Ranking 6,000 ticket-like items took 4 ms (`hcp-12`), 22 ms (`export`) and 60–75 ms
(`e`, `anna exp`, `xpt`) on a desktop CPU; filtering candidates is at most 16 ms of
that, matching the rest.

## GitHub builds and releases

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
