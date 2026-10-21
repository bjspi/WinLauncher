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

## GitHub builds and releases

[CI](https://github.com/bjspi/WinLauncher/actions/workflows/ci.yml) runs on pushes to
`main`, pull requests and manual dispatch. It checks Python style, Rust formatting
and Clippy, runs the Windows tests with each engine, tests the Rust wheel on
Python 3.11, 3.12, 3.13 and 3.14, checks the Qt/keyboard flow, and builds and
verifies the Python wheel/source distribution and the Windows x64 Rust wheel. Download them from the run's `winlauncher-windows-packages` artifact.

Pushing a `v*` tag runs the same checks and publishes a GitHub Release only after
they pass, attaching those packages and a clean source ZIP. The tag must match
`winlauncher/__init__.py`, `pyproject.toml`, `native/pyproject.toml` and
`native/Cargo.toml` (for example `v0.2.2`), and the `rust` extra in `pyproject.toml`
must point to that release's native wheel. Install both wheels from a release
with `python -m pip install <python-wheel> <native-wheel>` to enable Rust without
installing a compiler. Source ZIPs retain `native/` for local builds.

For example, publish version 0.2.2 from a clean, committed checkout with:

```powershell
git tag -a v0.2.2 -m "WinLauncher 0.2.2"
git push origin v0.2.2
```

The finished release appears under [Releases](https://github.com/bjspi/WinLauncher/releases).
