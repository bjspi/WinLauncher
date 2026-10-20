//! The memory index: a read-only copy of the catalog's entries for candidate scans.
//!
//! SQLite answers substring queries from its trigram index quickly, but the
//! fuzzy pass (`LIKE '%a%b%c%'`) and terms too short for trigrams scan the whole
//! table on every keystroke. The memory index keeps every column a candidate
//! row needs in compact text arenas, so those scans touch only a few megabytes
//! of contiguous names, and candidates borrow their text instead of copying it.
//!
//! Freshness: SQLite triggers increment `catalog_version.generation` on every
//! change to `entries` (see Python's `CatalogStore`). Each search reads the
//! version in the same read transaction that it then searches, and uses the
//! index only if it was loaded from exactly that version; otherwise the index
//! is reloaded from that transaction's snapshot. Usage counts and favorites are
//! small and change on every launch, so each search reads them fresh.

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError};
use std::time::Instant;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::{named_params, Connection};

use crate::plan::{Context, Pass, Plan};
use crate::query::{required, Candidate, Kind};
use crate::sql_error;
use crate::CancelFlag;

/// Columns of the entries statement, in order.
const ENTRY_COLUMNS: usize = 11;
/// How many entries a scan checks between two looks at the cancel flag.
const ENTRIES_PER_CANCEL_CHECK: usize = 1024;

/// The SQL the index loads from, as Python's `memory_index.MEMORY_INDEX_SQL` sends it.
pub(crate) struct CatalogSpec {
    pub database: String,
    version: String,
    entries: String,
    usage: String,
    query_usage: String,
    favorites: String,
}

impl CatalogSpec {
    pub fn from_python(catalog: &Bound<'_, PyDict>) -> PyResult<Self> {
        let statements: Bound<'_, PyDict> = required(catalog, "statements")?;
        Ok(Self {
            database: required(catalog, "database")?,
            version: required(&statements, "version")?,
            entries: required(&statements, "entries")?,
            usage: required(&statements, "usage")?,
            query_usage: required(&statements, "query_usage")?,
            favorites: required(&statements, "favorites")?,
        })
    }
}

/// Which catalog state an index copies; equal versions mean equal entries.
#[derive(Debug)]
struct Version {
    database: String,
    id: String,
    generation: i64,
    entries_sql: String,
}

fn read_version(connection: &Connection, spec: &CatalogSpec) -> PyResult<Version> {
    let (id, generation) = connection
        .query_row(&spec.version, [], |row| Ok((row.get(0)?, row.get(1)?)))
        .map_err(sql_error)?;
    Ok(Version {
        database: spec.database.clone(),
        id,
        generation,
        entries_sql: spec.entries.clone(),
    })
}

/// Plan columns; text columns borrow from the arenas.
#[derive(Clone, Copy, Debug)]
pub(crate) enum Column {
    PathKey,
    Kind,
    Name,
    SearchPath,
    Extension,
    Size,
    Modified,
}

impl Column {
    pub fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "path_key" => Self::PathKey,
            "kind" => Self::Kind,
            "name" => Self::Name,
            "search_path" => Self::SearchPath,
            "extension" => Self::Extension,
            "size" => Self::Size,
            "modified" => Self::Modified,
            _ => return None,
        })
    }

    pub fn is_text(self) -> bool {
        !matches!(self, Self::Size | Self::Modified)
    }
}

/// Many strings in one allocation; entry `i` spans `ends[i - 1]..ends[i]`.
#[derive(Default)]
struct TextArena {
    text: String,
    ends: Vec<u32>,
}

impl TextArena {
    fn push(&mut self, value: &str) -> PyResult<()> {
        self.text.push_str(value);
        let end = u32::try_from(self.text.len())
            .map_err(|_| PyValueError::new_err("The catalog is too large for the memory index"))?;
        self.ends.push(end);
        Ok(())
    }

    fn get(&self, entry: usize) -> &str {
        let start = entry
            .checked_sub(1)
            .map_or(0, |previous| self.ends[previous]);
        &self.text[start as usize..self.ends[entry] as usize]
    }

    fn shrink(&mut self) {
        self.text.shrink_to_fit();
        self.ends.shrink_to_fit();
    }

    fn bytes(&self) -> usize {
        self.text.capacity() + self.ends.capacity() * size_of::<u32>()
    }
}

/// Every entry's candidate columns except usage, in rowid order.
pub(crate) struct CatalogIndex {
    version: Version,
    rowids: Vec<i64>,
    paths: TextArena,
    identities: TextArena,
    names: TextArena,
    search_paths: TextArena,
    extensions: TextArena,
    kinds: Vec<Kind>,
    sizes: Vec<i64>,
    modified: Vec<f64>,
    attributes: Vec<i64>,
    application_sources: Vec<bool>,
}

/// LIKE compares ASCII case-insensitively; the index compares bytes, which is
/// the same only for case-folded text, as the catalog stores it.
fn require_folded(text: &str) -> PyResult<()> {
    if text.bytes().any(|byte| byte.is_ascii_uppercase()) {
        return Err(PyValueError::new_err(
            "The catalog holds search text that is not case-folded",
        ));
    }
    Ok(())
}

impl CatalogIndex {
    fn load(connection: &Connection, spec: &CatalogSpec, version: Version) -> PyResult<Self> {
        let mut index = Self {
            version,
            rowids: Vec::new(),
            paths: TextArena::default(),
            identities: TextArena::default(),
            names: TextArena::default(),
            search_paths: TextArena::default(),
            extensions: TextArena::default(),
            kinds: Vec::new(),
            sizes: Vec::new(),
            modified: Vec::new(),
            attributes: Vec::new(),
            application_sources: Vec::new(),
        };
        let mut statement = connection.prepare(&spec.entries).map_err(sql_error)?;
        if statement.column_count() != ENTRY_COLUMNS {
            return Err(PyValueError::new_err(
                "Expected an 11-column entries statement",
            ));
        }
        let mut rows = statement.raw_query();
        while let Some(row) = rows.next().map_err(sql_error)? {
            let text = |column: usize| -> PyResult<&str> {
                row.get_ref(column)
                    .and_then(|value| value.as_str().map_err(Into::into))
                    .map_err(sql_error)
            };
            let rowid: i64 = row.get(0).map_err(sql_error)?;
            if index.rowids.last().is_some_and(|last| *last >= rowid) {
                return Err(PyValueError::new_err("Entries must arrive in rowid order"));
            }
            let (name, search_path, extension) = (text(4)?, text(5)?, text(6)?);
            for folded in [name, search_path, extension] {
                require_folded(folded)?;
            }
            index.rowids.push(rowid);
            index.paths.push(text(1)?)?;
            index.identities.push(text(2)?)?;
            index.kinds.push(Kind::parse(text(3)?)?);
            index.names.push(name)?;
            index.search_paths.push(search_path)?;
            index.extensions.push(extension)?;
            index.sizes.push(row.get(7).map_err(sql_error)?);
            index.modified.push(row.get(8).map_err(sql_error)?);
            index.attributes.push(row.get(9).map_err(sql_error)?);
            index
                .application_sources
                .push(row.get(10).map_err(sql_error)?);
        }
        index.shrink();
        Ok(index)
    }

    fn shrink(&mut self) {
        for arena in self.arenas_mut() {
            arena.shrink();
        }
        self.rowids.shrink_to_fit();
        self.kinds.shrink_to_fit();
        self.sizes.shrink_to_fit();
        self.modified.shrink_to_fit();
        self.attributes.shrink_to_fit();
        self.application_sources.shrink_to_fit();
    }

    fn arenas_mut(&mut self) -> [&mut TextArena; 5] {
        [
            &mut self.paths,
            &mut self.identities,
            &mut self.names,
            &mut self.search_paths,
            &mut self.extensions,
        ]
    }

    pub fn len(&self) -> usize {
        self.rowids.len()
    }

    /// Heap memory of the index in bytes.
    pub fn bytes(&self) -> usize {
        let arenas = [
            &self.paths,
            &self.identities,
            &self.names,
            &self.search_paths,
            &self.extensions,
        ];
        arenas.iter().map(|arena| arena.bytes()).sum::<usize>()
            + self.rowids.capacity() * size_of::<i64>()
            + self.kinds.capacity() * size_of::<Kind>()
            + self.sizes.capacity() * size_of::<i64>()
            + self.modified.capacity() * size_of::<f64>()
            + self.attributes.capacity() * size_of::<i64>()
            + self.application_sources.capacity() * size_of::<bool>()
    }

    pub fn generation(&self) -> i64 {
        self.version.generation
    }

    pub fn rowid(&self, entry: usize) -> i64 {
        self.rowids[entry]
    }

    fn position(&self, rowid: i64) -> PyResult<usize> {
        self.rowids.binary_search(&rowid).map_err(|_| {
            PyValueError::new_err("The memory index does not match its catalog snapshot")
        })
    }

    pub fn text(&self, column: Column, entry: usize) -> &str {
        match column {
            Column::PathKey => self.identities.get(entry),
            Column::Kind => self.kinds[entry].as_str(),
            Column::Name => self.names.get(entry),
            Column::SearchPath => self.search_paths.get(entry),
            Column::Extension => self.extensions.get(entry),
            Column::Size | Column::Modified => unreachable!("not a text column"),
        }
    }

    pub fn number(&self, column: Column, entry: usize) -> f64 {
        match column {
            Column::Size => self.sizes[entry] as f64,
            Column::Modified => self.modified[entry],
            _ => unreachable!("not a number column"),
        }
    }

    /// The candidate row candidate SQL would return for this entry.
    pub fn candidate(&self, entry: usize, usage: Option<&Usage>) -> Candidate<'_> {
        let usage = usage.copied().unwrap_or_default();
        Candidate {
            path_text: Cow::Borrowed(self.paths.get(entry)),
            identity: Cow::Borrowed(self.identities.get(entry)),
            kind: self.kinds[entry],
            search_name: Cow::Borrowed(self.names.get(entry)),
            search_path: Cow::Borrowed(self.search_paths.get(entry)),
            extension: Cow::Borrowed(self.extensions.get(entry)),
            size: self.sizes[entry],
            modified: self.modified[entry],
            attributes: self.attributes[entry],
            launches: usage.launches,
            last_used: usage.last_used,
            query_launches: usage.query_launches,
            pinned: usage.pinned,
            application_source: self.application_sources[entry],
        }
    }
}

/// Whether a search may load a stale index itself or must leave it to SQL.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Load {
    /// Load now, waiting for a load another thread already started.
    Wait,
    /// Load now unless another thread is loading; then answer None at once.
    IfIdle,
}

/// The most recently loaded index. One slot: PathScout searches one catalog.
static CURRENT: Mutex<Option<Arc<CatalogIndex>>> = Mutex::new(None);
/// Held while an index loads, so concurrent searches never load twice.
static LOADING: Mutex<()> = Mutex::new(());

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // A panic while holding the lock leaves no partial state: slots are swapped whole.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// What the cache holds for one wanted catalog state.
enum Cached {
    Current(Arc<CatalogIndex>),
    /// A later state of the same catalog: the wanted snapshot predates a change.
    Newer,
    Missing,
}

fn cached(wanted: &Version) -> Cached {
    let slot = lock(&CURRENT);
    let Some(index) = slot.as_ref() else {
        return Cached::Missing;
    };
    let loaded = &index.version;
    let same_catalog = loaded.database == wanted.database
        && loaded.id == wanted.id
        && loaded.entries_sql == wanted.entries_sql;
    match loaded.generation.cmp(&wanted.generation) {
        _ if !same_catalog => Cached::Missing,
        std::cmp::Ordering::Equal => Cached::Current(Arc::clone(index)),
        std::cmp::Ordering::Greater => Cached::Newer,
        std::cmp::Ordering::Less => Cached::Missing,
    }
}

/// What `current` did, for diagnostics and the warm-up report.
pub(crate) struct Loaded {
    pub index: Arc<CatalogIndex>,
    pub reloaded: bool,
    pub milliseconds: f64,
}

/// The index for exactly the snapshot `connection`'s open read transaction sees.
///
/// None if `load` is `IfIdle` and another thread is loading, or if a later
/// state is loaded already (this transaction started before a change): the
/// newer copy is kept for the searches that follow, and this search uses SQL.
pub(crate) fn current(
    connection: &Connection,
    spec: &CatalogSpec,
    load: Load,
) -> PyResult<Option<Loaded>> {
    let started = Instant::now();
    let version = read_version(connection, spec)?;
    let found = |index| Loaded {
        index,
        reloaded: false,
        milliseconds: 0.0,
    };
    match cached(&version) {
        Cached::Current(index) => return Ok(Some(found(index))),
        Cached::Newer => return Ok(None),
        Cached::Missing => {}
    }
    let _loading = match load {
        Load::Wait => lock(&LOADING),
        Load::IfIdle => match LOADING.try_lock() {
            Ok(guard) => guard,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Ok(None),
        },
    };
    // Another thread may have loaded while this one waited for the lock.
    match cached(&version) {
        Cached::Current(index) => return Ok(Some(found(index))),
        Cached::Newer => return Ok(None),
        Cached::Missing => {}
    }
    // Drop the cache's reference first, so it never keeps two copies alive.
    lock(&CURRENT).take();
    let index = Arc::new(CatalogIndex::load(connection, spec, version)?);
    *lock(&CURRENT) = Some(Arc::clone(&index));
    Ok(Some(Loaded {
        index,
        reloaded: true,
        milliseconds: started.elapsed().as_secs_f64() * 1000.0,
    }))
}

/// Personal signals of one entry, as candidate SQL's usage joins provide them.
#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct Usage {
    launches: i64,
    last_used: f64,
    query_launches: i64,
    pinned: bool,
    /// Has a `usage` row or a favorite mark (the `learned` plan condition).
    learned: bool,
}

/// Everything one search reads besides the index: launches and favorites.
pub(crate) struct Selection<'a> {
    index: &'a CatalogIndex,
    usage: HashMap<usize, Usage>,
}

impl Context for Selection<'_> {
    fn is_learned(&self, entry: usize) -> bool {
        self.usage.get(&entry).is_some_and(|usage| usage.learned)
    }
}

impl<'a> Selection<'a> {
    pub fn prepare(
        connection: &Connection,
        spec: &CatalogSpec,
        index: &'a CatalogIndex,
        plan: &Plan,
    ) -> PyResult<Self> {
        let mut selection = Self {
            index,
            usage: HashMap::new(),
        };
        selection.read_usage(connection, spec, &plan.history_key)?;
        Ok(selection)
    }

    fn read_usage(
        &mut self,
        connection: &Connection,
        spec: &CatalogSpec,
        history_key: &str,
    ) -> PyResult<()> {
        let mut usage = connection.prepare(&spec.usage).map_err(sql_error)?;
        let mut rows = usage.query([]).map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let entry = self.entry(row.get(0).map_err(sql_error)?)?;
            entry.launches = row.get(1).map_err(sql_error)?;
            entry.last_used = row.get(2).map_err(sql_error)?;
            entry.learned = true;
        }
        let mut queries = connection.prepare(&spec.query_usage).map_err(sql_error)?;
        let mut rows = queries
            .query(named_params! {":history_key": history_key})
            .map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let entry = self.entry(row.get(0).map_err(sql_error)?)?;
            entry.query_launches = row.get(1).map_err(sql_error)?;
        }
        let mut favorites = connection.prepare(&spec.favorites).map_err(sql_error)?;
        let mut rows = favorites.query([]).map_err(sql_error)?;
        while let Some(row) = rows.next().map_err(sql_error)? {
            let entry = self.entry(row.get(0).map_err(sql_error)?)?;
            entry.pinned = true;
            entry.learned = true;
        }
        Ok(())
    }

    fn entry(&mut self, rowid: i64) -> PyResult<&mut Usage> {
        let position = self.index.position(rowid)?;
        Ok(self.usage.entry(position).or_default())
    }

    pub fn usage(&self, entry: usize) -> Option<&Usage> {
        self.usage.get(&entry)
    }

    /// Visit the entries `pass` selects in rowid order; false if cancelled.
    pub fn scan(
        &self,
        pass: &Pass,
        cancel: &CancelFlag,
        mut visit: impl FnMut(usize) -> PyResult<()>,
    ) -> PyResult<bool> {
        for entry in 0..self.index.len() {
            if entry.is_multiple_of(ENTRIES_PER_CANCEL_CHECK) && cancel.is_set() {
                return Ok(false);
            }
            if pass.holds(self.index, entry, self) {
                visit(entry)?;
            }
        }
        Ok(!cancel.is_set())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arenas_return_each_pushed_string() {
        let mut arena = TextArena::default();
        for text in ["", "ä🙂", r"c:\work", ""] {
            arena.push(text).unwrap();
        }
        let texts: Vec<&str> = (0..4).map(|entry| arena.get(entry)).collect();
        assert_eq!(texts, ["", "ä🙂", r"c:\work", ""]);
    }

    #[test]
    fn only_case_folded_text_is_accepted() {
        assert!(require_folded(r"c:\users\straße.txt").is_ok());
        assert!(require_folded("ärger").is_ok());
        assert!(require_folded("Report.txt").is_err());
    }
}
