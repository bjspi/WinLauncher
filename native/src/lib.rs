//! Optional search acceleration. Python owns SQL, exclusions and hit construction.

mod content;
mod content_workers;
mod fuzzy;
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
use rusqlite::{Connection, OpenFlags};

use content::ContentFilter;
use content_workers::ContentWorkers;
use heap::{BestHits, Kept};
use priority::Policy;
use query::{Candidate, Query};

const CANCEL_CHECK_INSTRUCTIONS: i32 = 1000;
type RowResult = (usize, f64, u8, Vec<String>);
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
        let content = query
            .content_text
            .as_deref()
            .map(ContentFilter::new)
            .transpose()
            .map_err(|err| PyValueError::new_err(err.to_string()))?
            .map(|filter| ContentWorkers::new(filter, query.content_search_threads, cancel))
            .transpose()?;
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
        if priority.search == "ignored" || !self.query.accepts(&row) {
            return Ok(());
        }
        if !self
            .best
            .could_keep(&heap::best_possible_key(self.query, &row, &priority))
        {
            return Ok(());
        }
        let Some(ranking) = rank::rank(self.query, &row, &priority) else {
            return Ok(());
        };
        let key = heap::key(self.query, &row, &ranking);
        if !self.best.would_keep(&key) || self.cancel.is_set() {
            return Ok(());
        }
        let allowed = Python::with_gil(|py| {
            self.allows
                .call1(py, (&row.path_text, row.kind == "folder"))?
                .extract::<bool>(py)
        })?;
        if allowed {
            let hit = Kept {
                key,
                index,
                row,
                ranking,
            };
            if let Some(content) = &mut self.content {
                if let Some(ready) = content.submit(hit)? {
                    self.keep(ready);
                }
            } else {
                self.best.add(hit);
            }
        }
        Ok(())
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
            Ok(Vec::new())
        } else {
            Ok(self.best.ordered())
        }
    }
}

#[pyfunction]
fn rank_rows(
    py: Python<'_>,
    query: &Bound<'_, PyDict>,
    priorities: &Bound<'_, PyDict>,
    rows: &Bound<'_, PyAny>,
    allows: Py<PyAny>,
    cancel: PyRef<'_, CancelFlag>,
) -> PyResult<Vec<RowResult>> {
    let query = Query::from_dict(query)?;
    let priorities = Policy::from_dict(priorities)?;
    let cancel = cancel.clone();
    let mut candidates = Vec::new();
    for row in rows.try_iter()? {
        if cancel.is_set() {
            return Ok(Vec::new());
        }
        candidates.push(Candidate::from_python(&row?)?);
        if candidates.len() % 1000 == 0 {
            py.allow_threads(std::thread::yield_now);
        }
    }
    let kept = py.allow_threads(move || {
        if priorities.has_name_rules() {
            for row in &candidates {
                if cancel.is_set() {
                    return Ok(Vec::new());
                }
                priorities.validate_candidate(row)?;
            }
        }
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
            (
                hit.index,
                hit.ranking.score,
                hit.ranking.band,
                hit.ranking.reasons,
            )
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

fn candidate_from_sql(row: &rusqlite::Row<'_>) -> Result<Candidate, rusqlite::Error> {
    Ok(Candidate {
        path_text: row.get(0)?,
        identity: row.get(1)?,
        kind: row.get(2)?,
        search_name: row.get(3)?,
        search_path: row.get(4)?,
        extension: row.get(5)?,
        size: row.get(6)?,
        modified: row.get(7)?,
        attributes: row.get(8)?,
        launches: row.get(9)?,
        last_used: row.get(10)?,
        query_launches: row.get(11)?,
        pinned: row.get(12)?,
    })
}

fn search_sqlite(
    database: &str,
    sql: &str,
    parameters: &[(String, Value)],
    query: &Query,
    priorities: &Policy,
    allows: &Py<PyAny>,
    cancel: &CancelFlag,
) -> PyResult<Vec<Kept>> {
    if cancel.is_set() {
        return Ok(Vec::new());
    }
    let connection = Connection::open_with_flags(database, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(sql_error)?;
    connection
        .busy_timeout(Duration::from_secs(15))
        .map_err(sql_error)?;
    let progress_flag = cancel.clone();
    connection.progress_handler(
        CANCEL_CHECK_INSTRUCTIONS,
        Some(move || progress_flag.is_set()),
    );
    connection.execute_batch("BEGIN").map_err(sql_error)?;
    let mut statement = connection.prepare(sql).map_err(sql_error)?;
    if !statement.readonly() || statement.column_count() != 13 {
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
    // Reject unsupported regex inputs before any disk callback can run.
    if priorities.has_name_rules() {
        let mut preflight = statement.raw_query();
        loop {
            if cancel.is_set() {
                return Ok(Vec::new());
            }
            match preflight.next() {
                Ok(Some(row)) => {
                    priorities.validate_candidate(&candidate_from_sql(row).map_err(sql_error)?)?
                }
                Ok(None) => break,
                Err(_) if cancel.is_set() => return Ok(Vec::new()),
                Err(error) => return Err(sql_error(error)),
            }
        }
    }
    let mut rows = statement.raw_query();
    let mut search = Search::new(query, priorities, allows, cancel)?;
    let mut index = 0;
    loop {
        if cancel.is_set() {
            return Ok(Vec::new());
        }
        match rows.next() {
            Ok(Some(row)) => {
                let candidate = candidate_from_sql(row).map_err(sql_error)?;
                candidate.validate()?;
                search.consider(index, candidate)?;
                index += 1;
            }
            Ok(None) => break,
            Err(_) if cancel.is_set() => return Ok(Vec::new()),
            Err(error) => return Err(sql_error(error)),
        }
    }
    search.finish()
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
    let parameters = parameters_from_python(parameters)?;
    let query = Query::from_dict(query)?;
    let priorities = Policy::from_dict(priorities)?;
    let cancel = cancel.clone();
    let kept = py.allow_threads(move || {
        search_sqlite(
            &database,
            &sql,
            &parameters,
            &query,
            &priorities,
            &allows,
            &cancel,
        )
    })?;
    kept.into_iter()
        .map(|hit| {
            Ok((
                hit.row.to_python(py)?,
                hit.ranking.score,
                hit.ranking.band,
                hit.ranking.reasons,
            ))
        })
        .collect()
}

#[pymodule]
fn pathscout_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("API_VERSION", 7)?;
    module.add("UNICODE_VERSION", unicode::UNICODE_VERSION)?;
    module.add_class::<CancelFlag>()?;
    module.add_function(wrap_pyfunction!(rank_rows, module)?)?;
    module.add_function(wrap_pyfunction!(rank_sqlite, module)?)?;
    Ok(())
}
