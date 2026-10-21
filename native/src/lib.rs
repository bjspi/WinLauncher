//! Optional search acceleration. Python owns SQL, exclusions and hit construction.
//!
//! Three entry points rank candidates: `rank_rows` takes rows Python fetched,
//! `rank_sqlite` runs Python's candidate SQL, and `rank_catalog` evaluates
//! Python's candidate plan against the memory index (`catalog`), which is the
//! fastest and the default.

mod catalog;
mod content;
mod content_workers;
mod fuzzy;
mod glob;
mod heap;
mod like;
mod matching;
mod plan;
mod priority;
mod query;
mod rank;
mod unicode;

use std::borrow::Cow;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use rusqlite::types::Value;
use rusqlite::{Connection, OpenFlags, Statement};

use catalog::{CatalogSpec, Load, Selection};
use content::ContentFilter;
use content_workers::ContentWorkers;
use heap::{BestHits, Kept};
use plan::Plan;
use priority::{Policy, SearchClass};
use query::{Candidate, Kind, Query, CANDIDATE_COLUMNS};

/// Bumped whenever the Python-facing contract changes; Python checks it on import.
const API_VERSION: u32 = 13;
const CANCEL_CHECK_INSTRUCTIONS: i32 = 1000;
/// Same as Python's `CatalogStore` connections.
const BUSY_TIMEOUT: Duration = Duration::from_secs(15);
/// Lets the GIL go regularly while Python rows are converted.
const ROWS_PER_GIL_YIELD: usize = 1000;
/// `warm_catalog` retries when the catalog changes while it loads.
const WARM_ATTEMPTS: usize = 3;

/// `(row index, score, band, reasons)` for `rank_rows`.
type RowResult = (usize, f64, u8, Vec<String>);
/// `(row, score, band, reasons)` for `rank_sqlite` and `rank_catalog`.
type SqliteResult = (Py<PyTuple>, f64, u8, Vec<String>);

#[pyclass]
#[derive(Clone, Default)]
struct CancelFlag {
    flag: Arc<AtomicBool>,
}

#[pymethods]
impl CancelFlag {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn set(&self) {
        self.flag.store(true, Ordering::Relaxed);
    }

    fn is_set(&self) -> bool {
        self.flag.load(Ordering::Relaxed)
    }
}

/// One search: filters, early discard, ranking, exclusion callback and the heap.
struct Search<'a> {
    query: &'a Query,
    priorities: &'a Policy,
    allows: &'a Py<PyAny>,
    cancel: &'a CancelFlag,
    best: BestHits,
    content: Option<ContentWorkers>,
}

impl<'a> Search<'a> {
    fn new(
        query: &'a Query,
        priorities: &'a Policy,
        allows: &'a Py<PyAny>,
        cancel: &'a CancelFlag,
    ) -> PyResult<Self> {
        let content = match query.content_text.as_deref() {
            Some(text) => {
                let filter = ContentFilter::new(text)
                    .map_err(|err| PyValueError::new_err(err.to_string()))?;
                Some(ContentWorkers::new(
                    filter,
                    query.content_search_threads,
                    cancel,
                )?)
            }
            None => None,
        };
        Ok(Self {
            query,
            priorities,
            allows,
            cancel,
            best: BestHits::new(query.limit),
            content,
        })
    }

    fn consider(&mut self, index: usize, row: Candidate<'_>) -> PyResult<()> {
        let priority = self.priorities.evaluate(&row)?;
        if priority.class == SearchClass::Ignored || !self.query.accepts(&row) {
            return Ok(());
        }
        let best_possible = heap::best_possible_key(self.query, &row, &priority);
        if !self.best.could_keep(&best_possible) {
            return Ok(());
        }
        let Some(ranking) = rank::rank(self.query, &row, &priority) else {
            return Ok(());
        };
        let key = heap::key(self.query, &row, &ranking);
        if !self.best.would_keep(&key) || self.cancel.is_set() || !self.allows(&row)? {
            return Ok(());
        }
        let hit = Kept {
            key,
            index,
            row: row.into_owned(),
            ranking,
        };
        match &mut self.content {
            Some(content) => {
                if let Some(ready) = content.submit(hit)? {
                    self.keep(ready);
                }
            }
            None => self.best.add(hit),
        }
        Ok(())
    }

    /// Python's exclusion check; it probes the disk for marker files.
    fn allows(&self, row: &Candidate) -> PyResult<bool> {
        let arguments = (
            &*row.path_text,
            row.kind == Kind::Folder,
            row.application_source,
        );
        Python::with_gil(|py| self.allows.call1(py, arguments)?.extract::<bool>(py))
    }

    fn keep(&mut self, hit: Kept) {
        if self.best.would_keep(&hit.key) {
            self.best.add(hit);
        }
    }

    fn finish(mut self) -> PyResult<Vec<Kept>> {
        while let Some(content) = &mut self.content {
            if content.is_empty() || self.cancel.is_set() {
                break;
            }
            if let Some(ready) = content.next()? {
                self.keep(ready);
            }
        }
        if self.cancel.is_set() {
            return Ok(Vec::new());
        }
        Ok(self.best.ordered())
    }
}

fn require_unicode_tables() -> PyResult<()> {
    if unicode::is_selected() {
        Ok(())
    } else {
        Err(PyRuntimeError::new_err(
            "Call use_unicode() before searching",
        ))
    }
}

/// Phase 1: rank candidate rows that Python already fetched.
#[pyfunction]
fn rank_rows(
    py: Python<'_>,
    query: &Bound<'_, PyDict>,
    priorities: &Bound<'_, PyDict>,
    rows: &Bound<'_, PyAny>,
    allows: Py<PyAny>,
    cancel: PyRef<'_, CancelFlag>,
) -> PyResult<Vec<RowResult>> {
    require_unicode_tables()?;
    let query = Query::from_dict(query)?;
    let priorities = Policy::from_dict(priorities)?;
    let cancel = cancel.clone();
    let mut candidates = Vec::new();
    for row in rows.try_iter()? {
        if cancel.is_set() {
            return Ok(Vec::new());
        }
        candidates.push(Candidate::from_python(&row?)?);
        if candidates.len() % ROWS_PER_GIL_YIELD == 0 {
            py.allow_threads(std::thread::yield_now);
        }
    }
    let kept = py.allow_threads(move || {
        let mut search = Search::new(&query, &priorities, &allows, &cancel)?;
        for (index, candidate) in candidates.into_iter().enumerate() {
            if cancel.is_set() {
                return Ok(Vec::new());
            }
            search.consider(index, candidate)?;
        }
        search.finish()
    })?;
    Ok(kept
        .into_iter()
        .map(|hit| {
            let band = hit.ranking.band as u8;
            (hit.index, hit.ranking.score, band, hit.ranking.reasons)
        })
        .collect())
}

pub(crate) fn sql_error(error: rusqlite::Error) -> PyErr {
    PyRuntimeError::new_err(format!("Native SQLite search: {error}"))
}

fn parameters_from_python(parameters: &Bound<'_, PyDict>) -> PyResult<Vec<(String, Value)>> {
    parameters
        .iter()
        .map(|(key, value)| {
            let name: String = key.extract()?;
            let value = if value.is_none() {
                Value::Null
            } else if let Ok(value) = value.extract::<i64>() {
                Value::Integer(value)
            } else if let Ok(value) = value.extract::<f64>() {
                if !value.is_finite() {
                    return Err(PyValueError::new_err("Non-finite SQL parameter"));
                }
                Value::Real(value)
            } else {
                Value::Text(value.extract()?)
            };
            Ok((format!(":{name}"), value))
        })
        .collect()
}

fn candidate_from_sql(row: &rusqlite::Row<'_>) -> PyResult<Candidate<'static>> {
    let kind: String = row.get(2).map_err(sql_error)?;
    let text = |index: usize| -> PyResult<Cow<'static, str>> {
        Ok(Cow::Owned(row.get(index).map_err(sql_error)?))
    };
    let candidate = Candidate {
        path_text: text(0)?,
        identity: text(1)?,
        kind: Kind::parse(&kind)?,
        search_name: text(3)?,
        search_path: text(4)?,
        extension: text(5)?,
        size: row.get(6).map_err(sql_error)?,
        modified: row.get(7).map_err(sql_error)?,
        attributes: row.get(8).map_err(sql_error)?,
        launches: row.get(9).map_err(sql_error)?,
        last_used: row.get(10).map_err(sql_error)?,
        query_launches: row.get(11).map_err(sql_error)?,
        pinned: row.get(12).map_err(sql_error)?,
        application_source: row.get(13).map_err(sql_error)?,
    };
    candidate.validate()?;
    Ok(candidate)
}

/// A read-only connection in a read transaction; with `cancel`, its statements
/// stop once the search is cancelled.
fn open_read_only(database: &str, cancel: Option<&CancelFlag>) -> PyResult<Connection> {
    let connection = Connection::open_with_flags(database, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(sql_error)?;
    connection.busy_timeout(BUSY_TIMEOUT).map_err(sql_error)?;
    if let Some(flag) = cancel.cloned() {
        connection.progress_handler(CANCEL_CHECK_INSTRUCTIONS, Some(move || flag.is_set()));
    }
    // One read transaction, so both candidate passes see the same snapshot.
    connection.execute_batch("BEGIN").map_err(sql_error)?;
    Ok(connection)
}

/// Bind Python's named parameters; every placeholder must have a value.
fn bind_parameters(statement: &mut Statement<'_>, parameters: &[(String, Value)]) -> PyResult<()> {
    if !statement.readonly() || statement.column_count() != CANDIDATE_COLUMNS {
        return Err(PyValueError::new_err(
            "Expected a read-only candidate SELECT",
        ));
    }
    for (name, value) in parameters {
        if let Some(index) = statement.parameter_index(name).map_err(sql_error)? {
            statement
                .raw_bind_parameter(index, value)
                .map_err(sql_error)?;
        }
    }
    for index in 1..=statement.parameter_count() {
        let name = statement
            .parameter_name(index)
            .ok_or_else(|| PyValueError::new_err("Expected named SQL parameters"))?;
        if !parameters.iter().any(|(provided, _)| provided == name) {
            return Err(PyValueError::new_err(format!(
                "Missing SQL parameter {name}"
            )));
        }
    }
    Ok(())
}

/// Phase 2: run Python's candidate SQL and rank the rows as they stream in.
fn search_sqlite(
    database: &str,
    sql: &str,
    parameters: &[(String, Value)],
    search: Search<'_>,
    cancel: &CancelFlag,
) -> PyResult<Vec<Kept>> {
    let mut search = search;
    if cancel.is_set() {
        return Ok(Vec::new());
    }
    let connection = open_read_only(database, Some(cancel))?;
    let mut statement = connection.prepare(sql).map_err(sql_error)?;
    bind_parameters(&mut statement, parameters)?;
    let mut rows = statement.raw_query();
    let mut index = 0;
    loop {
        if cancel.is_set() {
            return Ok(Vec::new());
        }
        match rows.next() {
            Ok(Some(row)) => {
                search.consider(index, candidate_from_sql(row)?)?;
                index += 1;
            }
            Ok(None) => return search.finish(),
            // The progress handler interrupts a cancelled statement with an error.
            Err(_) if cancel.is_set() => return Ok(Vec::new()),
            Err(error) => return Err(sql_error(error)),
        }
    }
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn rank_sqlite(
    py: Python<'_>,
    database: String,
    sql: String,
    parameters: &Bound<'_, PyDict>,
    query: &Bound<'_, PyDict>,
    priorities: &Bound<'_, PyDict>,
    allows: Py<PyAny>,
    cancel: PyRef<'_, CancelFlag>,
) -> PyResult<Vec<SqliteResult>> {
    require_unicode_tables()?;
    let parameters = parameters_from_python(parameters)?;
    let query = Query::from_dict(query)?;
    let priorities = Policy::from_dict(priorities)?;
    let cancel = cancel.clone();
    let kept = py.allow_threads(|| {
        let search = Search::new(&query, &priorities, &allows, &cancel)?;
        search_sqlite(&database, &sql, &parameters, search, &cancel)
    })?;
    hits_to_python(py, kept)
}

fn hits_to_python(py: Python<'_>, kept: Vec<Kept>) -> PyResult<Vec<SqliteResult>> {
    kept.into_iter()
        .map(|hit| {
            let band = hit.ranking.band as u8;
            Ok((
                hit.row.to_python(py)?,
                hit.ranking.score,
                band,
                hit.ranking.reasons,
            ))
        })
        .collect()
}

/// A read transaction on the catalog with the memory index for its snapshot.
///
/// Cancelling does not interrupt a reload: the next search needs the index too.
/// The cancel handler is installed only afterwards, for the full-text queries.
fn open_catalog(
    spec: &CatalogSpec,
    load: Load,
    cancel: &CancelFlag,
) -> PyResult<Option<(Connection, catalog::Loaded)>> {
    let connection = open_read_only(&spec.database, None)?;
    let Some(loaded) = catalog::current(&connection, spec, load)? else {
        return Ok(None);
    };
    let flag = cancel.clone();
    connection.progress_handler(CANCEL_CHECK_INSTRUCTIONS, Some(move || flag.is_set()));
    Ok(Some((connection, loaded)))
}

/// Phase 3: evaluate Python's candidate plan against the memory index.
/// None while another thread loads the index; the caller then uses SQL.
fn search_catalog(
    spec: &CatalogSpec,
    plan: &Plan,
    mut search: Search<'_>,
    cancel: &CancelFlag,
) -> PyResult<Option<Vec<Kept>>> {
    if cancel.is_set() {
        return Ok(Some(Vec::new()));
    }
    let Some((connection, loaded)) = open_catalog(spec, Load::IfIdle, cancel)? else {
        return Ok(None);
    };
    let index = &loaded.index;
    let selection = match Selection::prepare(&connection, spec, index, plan) {
        Ok(selection) => selection,
        Err(_) if cancel.is_set() => return Ok(Some(Vec::new())),
        Err(error) => return Err(error),
    };
    for pass in std::iter::once(&plan.literal).chain(&plan.fuzzy) {
        let finished = selection.scan(pass, cancel, |entry| {
            search.consider(entry, index.candidate(entry, selection.usage(entry)))
        })?;
        if !finished {
            return Ok(Some(Vec::new()));
        }
    }
    search.finish().map(Some)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn rank_catalog(
    py: Python<'_>,
    catalog: &Bound<'_, PyDict>,
    plan: &Bound<'_, PyDict>,
    query: &Bound<'_, PyDict>,
    priorities: &Bound<'_, PyDict>,
    allows: Py<PyAny>,
    cancel: PyRef<'_, CancelFlag>,
) -> PyResult<Option<Vec<SqliteResult>>> {
    require_unicode_tables()?;
    let spec = CatalogSpec::from_python(catalog)?;
    let plan = Plan::from_python(plan)?;
    let query = Query::from_dict(query)?;
    let priorities = Policy::from_dict(priorities)?;
    let cancel = cancel.clone();
    let kept = py.allow_threads(|| {
        let search = Search::new(&query, &priorities, &allows, &cancel)?;
        search_catalog(&spec, &plan, search, &cancel)
    })?;
    kept.map(|kept| hits_to_python(py, kept)).transpose()
}

/// The rowids each pass of `plan` selects, in scan order; for tests and benchmarks.
#[pyfunction]
fn select_catalog(
    py: Python<'_>,
    catalog: &Bound<'_, PyDict>,
    plan: &Bound<'_, PyDict>,
    cancel: PyRef<'_, CancelFlag>,
) -> PyResult<Option<(Vec<i64>, Vec<i64>)>> {
    let spec = CatalogSpec::from_python(catalog)?;
    let plan = Plan::from_python(plan)?;
    let cancel = cancel.clone();
    py.allow_threads(|| {
        let Some((connection, loaded)) = open_catalog(&spec, Load::IfIdle, &cancel)? else {
            return Ok(None);
        };
        let index = &loaded.index;
        let selection = Selection::prepare(&connection, &spec, index, &plan)?;
        let mut passes = (Vec::new(), Vec::new());
        selection.scan(&plan.literal, &cancel, |entry| {
            passes.0.push(index.rowid(entry));
            Ok(())
        })?;
        if let Some(fuzzy) = &plan.fuzzy {
            selection.scan(fuzzy, &cancel, |entry| {
                passes.1.push(index.rowid(entry));
                Ok(())
            })?;
        }
        Ok(Some(passes))
    })
}

/// Load the memory index for the catalog's current state unless it is loaded
/// already; waits for a load in progress. Call it after the catalog changed.
#[pyfunction]
fn warm_catalog(py: Python<'_>, catalog: &Bound<'_, PyDict>) -> PyResult<Py<PyDict>> {
    let spec = CatalogSpec::from_python(catalog)?;
    let started = Instant::now();
    let loaded = py.allow_threads(|| {
        // None only if a later catalog state was loaded meanwhile; look again.
        for _ in 0..WARM_ATTEMPTS {
            if let Some((_, loaded)) = open_catalog(&spec, Load::Wait, &CancelFlag::default())? {
                return Ok(loaded);
            }
        }
        Err(PyRuntimeError::new_err(
            "The catalog changed during every load attempt",
        ))
    })?;
    let report = PyDict::new(py);
    report.set_item("entries", loaded.index.len())?;
    report.set_item("bytes", loaded.index.bytes())?;
    report.set_item("generation", loaded.index.generation())?;
    report.set_item("reloaded", loaded.reloaded)?;
    report.set_item("load_ms", loaded.milliseconds)?;
    report.set_item("total_ms", started.elapsed().as_secs_f64() * 1000.0)?;
    Ok(report.unbind())
}

/// Select the tables of the calling interpreter's `unicodedata.unidata_version`.
#[pyfunction]
fn use_unicode(version: &str) -> PyResult<()> {
    unicode::select(version).map_err(PyValueError::new_err)
}

#[pymodule]
fn winlauncher_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("API_VERSION", API_VERSION)?;
    module.add("UNICODE_VERSIONS", unicode::supported_versions())?;
    module.add_class::<CancelFlag>()?;
    module.add_function(wrap_pyfunction!(use_unicode, module)?)?;
    module.add_function(wrap_pyfunction!(rank_rows, module)?)?;
    module.add_function(wrap_pyfunction!(rank_sqlite, module)?)?;
    module.add_function(wrap_pyfunction!(rank_catalog, module)?)?;
    module.add_function(wrap_pyfunction!(select_catalog, module)?)?;
    module.add_function(wrap_pyfunction!(warm_catalog, module)?)?;
    Ok(())
}
