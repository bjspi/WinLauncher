use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::matching::Term;

pub(crate) fn required<'py, T: FromPyObject<'py>>(
    dict: &Bound<'py, PyDict>,
    key: &str,
) -> PyResult<T> {
    dict.get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("Missing {key}")))?
        .extract()
}

pub(crate) struct Query {
    pub terms: Vec<Term>,
    pub folder_parts: Vec<Term>,
    pub excluded_terms: Vec<String>,
    pub kind: Option<String>,
    pub extensions: Vec<String>,
    pub scope_key: Option<String>,
    pub modified_after: Option<f64>,
    pub modified_before: Option<f64>,
    pub min_size: Option<i64>,
    pub max_size: Option<i64>,
    pub basename_only: bool,
    pub fuzzy: bool,
    pub combined: bool,
    pub sort: String,
    pub limit: usize,
    pub now: f64,
}

impl Query {
    pub fn from_dict(dict: &Bound<'_, PyDict>) -> PyResult<Self> {
        let query = Self {
            terms: compile_terms(required(dict, "terms")?)?,
            folder_parts: compile_terms(required(dict, "folder_parts")?)?,
            excluded_terms: required(dict, "excluded_terms")?,
            kind: required(dict, "kind")?,
            extensions: required(dict, "extensions")?,
            scope_key: required(dict, "scope_key")?,
            modified_after: required(dict, "modified_after")?,
            modified_before: required(dict, "modified_before")?,
            min_size: required(dict, "min_size")?,
            max_size: required(dict, "max_size")?,
            basename_only: required(dict, "basename_only")?,
            fuzzy: required(dict, "fuzzy")?,
            combined: required(dict, "combined")?,
            sort: required(dict, "sort")?,
            limit: required(dict, "limit")?,
            now: required(dict, "now")?,
        };
        if !(10..=200).contains(&query.limit)
            || !["score", "name", "modified"].contains(&query.sort.as_str())
            || !query.now.is_finite()
            || query.modified_after.is_some_and(|v| !v.is_finite())
            || query.modified_before.is_some_and(|v| !v.is_finite())
            || query.min_size.is_some_and(|v| v < 0)
            || query.max_size.is_some_and(|v| v < 0)
            || matches!((query.min_size, query.max_size), (Some(lower), Some(upper)) if lower > upper)
        {
            return Err(PyValueError::new_err("Invalid native query"));
        }
        Ok(query)
    }

    pub fn accepts(&self, row: &Candidate) -> bool {
        if self.kind.as_ref().is_some_and(|kind| *kind != row.kind)
            || (!self.extensions.is_empty() && !self.extensions.contains(&row.extension))
            || self
                .scope_key
                .as_ref()
                .is_some_and(|scope| row.identity == *scope || !row.identity.starts_with(scope))
            || self
                .excluded_terms
                .iter()
                .any(|term| row.search_path.contains(term))
            || self
                .modified_after
                .is_some_and(|after| row.modified < after)
            || self
                .modified_before
                .is_some_and(|before| row.modified >= before)
            || ((self.min_size.is_some() || self.max_size.is_some()) && row.kind == "folder")
            || self.min_size.is_some_and(|lower| row.size < lower)
            || self.max_size.is_some_and(|upper| row.size > upper)
        {
            return false;
        }
        true
    }
}

fn compile_terms(texts: Vec<String>) -> PyResult<Vec<Term>> {
    texts
        .into_iter()
        .map(|text| Term::new(text).map_err(|err| PyValueError::new_err(err.to_string())))
        .collect()
}

#[derive(Clone, Debug)]
pub(crate) struct Candidate {
    pub path_text: String,
    pub identity: String,
    pub kind: String,
    pub search_name: String,
    pub search_path: String,
    pub extension: String,
    pub size: i64,
    pub modified: f64,
    pub attributes: i64,
    pub launches: i64,
    pub last_used: f64,
    pub query_launches: i64,
    pub pinned: bool,
}

impl Candidate {
    pub fn from_python(row: &Bound<'_, PyAny>) -> PyResult<Self> {
        let row = row.downcast::<PyTuple>()?;
        if row.len() != 13 {
            return Err(PyValueError::new_err("Expected a 13-column candidate row"));
        }
        let candidate = Self {
            path_text: row.get_item(0)?.extract()?,
            identity: row.get_item(1)?.extract()?,
            kind: row.get_item(2)?.extract()?,
            search_name: row.get_item(3)?.extract()?,
            search_path: row.get_item(4)?.extract()?,
            extension: row.get_item(5)?.extract()?,
            size: row.get_item(6)?.extract()?,
            modified: row.get_item(7)?.extract()?,
            attributes: row.get_item(8)?.extract()?,
            launches: row.get_item(9)?.extract()?,
            last_used: row.get_item(10)?.extract()?,
            query_launches: row.get_item(11)?.extract()?,
            pinned: row.get_item(12)?.is_truthy()?,
        };
        candidate.validate()?;
        Ok(candidate)
    }

    pub fn validate(&self) -> PyResult<()> {
        if !["file", "folder", "app"].contains(&self.kind.as_str())
            || !self.modified.is_finite()
            || !self.last_used.is_finite()
            || self.launches < 0
            || self.query_launches < 0
        {
            return Err(PyValueError::new_err("Invalid native candidate"));
        }
        Ok(())
    }

    pub fn file_name(&self) -> &str {
        self.path_text.rsplit(['/', '\\']).next().unwrap_or("")
    }

    pub fn stem(&self) -> &str {
        let name = self.file_name();
        match name.rfind('.') {
            Some(dot) if dot > 0 && dot < name.len() - 1 => &name[..dot],
            _ => name,
        }
    }

    pub fn name(&self) -> &str {
        if self.kind == "app" {
            self.stem()
        } else {
            self.file_name()
        }
    }

    pub fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        let fields: Vec<PyObject> = vec![
            self.path_text
                .clone()
                .into_pyobject(py)?
                .unbind()
                .into_any(),
            self.identity.clone().into_pyobject(py)?.unbind().into_any(),
            self.kind.clone().into_pyobject(py)?.unbind().into_any(),
            self.search_name
                .clone()
                .into_pyobject(py)?
                .unbind()
                .into_any(),
            self.search_path
                .clone()
                .into_pyobject(py)?
                .unbind()
                .into_any(),
            self.extension
                .clone()
                .into_pyobject(py)?
                .unbind()
                .into_any(),
            self.size.into_pyobject(py)?.unbind().into_any(),
            self.modified.into_pyobject(py)?.unbind().into_any(),
            self.attributes.into_pyobject(py)?.unbind().into_any(),
            self.launches.into_pyobject(py)?.unbind().into_any(),
            self.last_used.into_pyobject(py)?.unbind().into_any(),
            self.query_launches.into_pyobject(py)?.unbind().into_any(),
            (self.pinned as u8).into_pyobject(py)?.unbind().into_any(),
        ];
        Ok(PyTuple::new(py, fields)?.unbind())
    }
}
