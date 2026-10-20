//! Optional search acceleration. Python owns SQL, exclusions and hit construction.

mod content;
mod content_workers;
mod fuzzy;
mod glob;
mod heap;
mod matching;
mod priority;
mod query;
mod rank;
mod unicode;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};
use rusqlite::types::Value;
use rusqlite::{Connection, OpenFlags, Statement};

use content::ContentFilter;
use content_workers::ContentWorkers;
use heap::{BestHits, Kept};
use priority::{Policy, SearchClass};
use query::{Candidate, Kind, Query, CANDIDATE_COLUMNS};

/// Bumped whenever the Python-facing contract changes; Python checks it on import.
const API_VERSION: u32 = 9;
const CANCEL_CHECK_INSTRUCTIONS: i32 = 1000;
/// Same as Python's `CatalogStore` connections.
const BUSY_TIMEOUT: Duration = Duration::from_secs(15);
/// Lets the GIL go regularly while Python rows are converted.
const ROWS_PER_GIL_YIELD: usize = 1000;

/// `(row index, score, band, reasons)` for `rank_rows`.
type RowResult = (usize, f64, u8, Vec<String>);
/// `(row, score, band, reasons)` for `rank_sqlite`.
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

    fn consider(&mut self, index: usize, row: Candidate) -> PyResult<()> {
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
            row,
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
            row.path_text.as_str(),
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

fn sql_error(error: rusqlite::Error) -> PyErr {
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

fn candidate_from_sql(row: &rusqlite::Row<'_>) -> PyResult<Candidate> {
    let kind: String = row.get(2).map_err(sql_error)?;
    let candidate = Candidate {
        path_text: row.get(0).map_err(sql_error)?,
        identity: row.get(1).map_err(sql_error)?,
        kind: Kind::parse(&kind)?,
        search_name: row.get(3).map_err(sql_error)?,
        search_path: row.get(4).map_err(sql_error)?,
        extension: row.get(5).map_err(sql_error)?,
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

/// A read-only connection whose statements stop once the search is cancelled.
fn open_read_only(database: &str, cancel: &CancelFlag) -> PyResult<Connection> {
    let connection = Connection::open_with_flags(database, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(sql_error)?;
    connection.busy_timeout(BUSY_TIMEOUT).map_err(sql_error)?;
    let flag = cancel.clone();
    connection.progress_handler(CANCEL_CHECK_INSTRUCTIONS, Some(move || flag.is_set()));
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
    let connection = open_read_only(database, cancel)?;
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

/// Select the tables of the calling interpreter's `unicodedata.unidata_version`.
#[pyfunction]
fn use_unicode(version: &str) -> PyResult<()> {
    unicode::select(version).map_err(PyValueError::new_err)
}

#[pymodule]
fn pathscout_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("API_VERSION", API_VERSION)?;
    module.add("UNICODE_VERSIONS", unicode::supported_versions())?;
    module.add_class::<CancelFlag>()?;
    module.add_function(wrap_pyfunction!(use_unicode, module)?)?;
    module.add_function(wrap_pyfunction!(rank_rows, module)?)?;
    module.add_function(wrap_pyfunction!(rank_sqlite, module)?)?;
    Ok(())
}
