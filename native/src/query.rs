//! The query and candidate rows exchanged with Python.

use std::borrow::Cow;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::matching::Term;

/// Columns of a candidate row; Python's `candidate_sql` selects them in this order.
pub(crate) const CANDIDATE_COLUMNS: usize = 14;
const LIMIT_RANGE: std::ops::RangeInclusive<usize> = 10..=200;
const CONTENT_THREAD_RANGE: std::ops::RangeInclusive<usize> = 1..=16;
const MAX_CONTENT_TEXT_CHARACTERS: usize = 128;

pub(crate) fn required<'py, T: FromPyObject<'py>>(
    dict: &Bound<'py, PyDict>,
    key: &str,
) -> PyResult<T> {
    dict.get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("Missing {key}")))?
        .extract()
}

fn invalid(message: &str) -> PyErr {
    PyValueError::new_err(format!("Invalid native query: {message}"))
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Kind {
    File,
    Folder,
    App,
}

impl Kind {
    pub fn parse(value: &str) -> PyResult<Self> {
        match value {
            "file" => Ok(Self::File),
            "folder" => Ok(Self::Folder),
            "app" => Ok(Self::App),
            _ => Err(PyValueError::new_err(format!(
                "Invalid item kind {value:?}"
            ))),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::File => "file",
            Self::Folder => "folder",
            Self::App => "app",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SortOrder {
    Score,
    Name,
    Modified,
}

impl SortOrder {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "score" => Ok(Self::Score),
            "name" => Ok(Self::Name),
            "modified" => Ok(Self::Modified),
            _ => Err(invalid("sort must be score, name or modified")),
        }
    }
}

pub(crate) struct Query {
    pub terms: Vec<Term>,
    pub folder_parts: Vec<Term>,
    pub excluded_terms: Vec<String>,
    pub kind: Option<Kind>,
    pub extensions: Vec<Term>,
    pub excluded_extensions: Vec<Term>,
    pub scope_key: Option<String>,
    /// Normalized scope path plus separator; Explorer searches match paths below it.
    pub scope_search_prefix: Option<String>,
    pub modified_after: Option<f64>,
    pub modified_before: Option<f64>,
    pub min_size: Option<i64>,
    pub max_size: Option<i64>,
    pub content_text: Option<String>,
    pub content_search_threads: usize,
    pub explorer: bool,
    pub basename_only: bool,
    pub fuzzy: bool,
    pub combined: bool,
    pub sort: SortOrder,
    pub limit: usize,
    pub now: f64,
}

impl Query {
    pub fn from_dict(dict: &Bound<'_, PyDict>) -> PyResult<Self> {
        let kind: Option<String> = required(dict, "kind")?;
        let sort: String = required(dict, "sort")?;
        let exact_terms: Vec<String> = required(dict, "exact_terms")?;
        let mut terms = compile_terms(required(dict, "terms")?)?;
        for term in &mut terms {
            term.exact = exact_terms.contains(&term.text);
        }
        let query = Self {
            terms,
            folder_parts: compile_terms(required(dict, "folder_parts")?)?,
            excluded_terms: required(dict, "excluded_terms")?,
            kind: kind.as_deref().map(Kind::parse).transpose()?,
            extensions: compile_terms(required(dict, "extensions")?)?,
            excluded_extensions: compile_terms(required(dict, "excluded_extensions")?)?,
            scope_key: required(dict, "scope_key")?,
            scope_search_prefix: required(dict, "scope_search_prefix")?,
            modified_after: required(dict, "modified_after")?,
            modified_before: required(dict, "modified_before")?,
            min_size: required(dict, "min_size")?,
            max_size: required(dict, "max_size")?,
            content_text: required(dict, "content_text")?,
            content_search_threads: required(dict, "content_search_threads")?,
            explorer: required(dict, "explorer")?,
            basename_only: required(dict, "basename_only")?,
            fuzzy: required(dict, "fuzzy")?,
            combined: required(dict, "combined")?,
            sort: SortOrder::parse(&sort)?,
            limit: required(dict, "limit")?,
            now: required(dict, "now")?,
        };
        query.validate()?;
        Ok(query)
    }

    fn validate(&self) -> PyResult<()> {
        if !LIMIT_RANGE.contains(&self.limit) {
            return Err(invalid("limit must be between 10 and 200"));
        }
        if !CONTENT_THREAD_RANGE.contains(&self.content_search_threads) {
            return Err(invalid("content_search_threads must be between 1 and 16"));
        }
        let times = [Some(self.now), self.modified_after, self.modified_before];
        if times.into_iter().flatten().any(|time| !time.is_finite()) {
            return Err(invalid("times must be finite"));
        }
        if self.min_size.is_some_and(|size| size < 0) || self.max_size.is_some_and(|size| size < 0)
        {
            return Err(invalid("sizes must not be negative"));
        }
        if matches!((self.min_size, self.max_size), (Some(lower), Some(upper)) if lower > upper) {
            return Err(invalid("min_size exceeds max_size"));
        }
        if let Some(text) = &self.content_text {
            let too_long = text.chars().count() > MAX_CONTENT_TEXT_CHARACTERS;
            if text.trim().is_empty() || too_long || text.chars().any(|c| c < ' ') {
                return Err(invalid("content_text must be 1-128 printable characters"));
            }
        }
        Ok(())
    }

    /// Structured filters, as Python's `StructuredFilters.accepts` (plus content files only).
    pub fn accepts(&self, row: &Candidate) -> bool {
        self.matches_type(row)
            && self.matches_location(row)
            && self.matches_date(row)
            && self.matches_size(row)
    }

    fn matches_type(&self, row: &Candidate) -> bool {
        if self.kind.is_some_and(|kind| kind != row.kind) {
            return false;
        }
        if self.content_text.is_some() && row.kind != Kind::File {
            return false;
        }
        if self
            .excluded_extensions
            .iter()
            .any(|term| term.matches(&row.extension))
        {
            return false;
        }
        self.extensions.is_empty()
            || self
                .extensions
                .iter()
                .any(|term| term.matches(&row.extension))
    }

    fn matches_location(&self, row: &Candidate) -> bool {
        if let Some(scope) = &self.scope_key {
            if row.identity == *scope || !row.identity.starts_with(scope) {
                return false;
            }
        }
        !self
            .excluded_terms
            .iter()
            .any(|term| row.search_path.contains(term.as_str()))
    }

    fn matches_date(&self, row: &Candidate) -> bool {
        self.modified_after
            .is_none_or(|after| row.modified >= after)
            && self
                .modified_before
                .is_none_or(|before| row.modified < before)
    }

    fn matches_size(&self, row: &Candidate) -> bool {
        if self.min_size.is_none() && self.max_size.is_none() {
            return true;
        }
        row.kind != Kind::Folder
            && self.min_size.is_none_or(|lower| row.size >= lower)
            && self.max_size.is_none_or(|upper| row.size <= upper)
    }

    /// The path text that "path contains" matching sees.
    pub fn matching_path<'a>(&self, row: &'a Candidate) -> &'a str {
        match &self.scope_search_prefix {
            Some(prefix) => row
                .search_path
                .strip_prefix(prefix.as_str())
                .unwrap_or(&row.search_path),
            None => &row.search_path,
        }
    }
}

fn compile_terms(texts: Vec<String>) -> PyResult<Vec<Term>> {
    texts
        .into_iter()
        .map(|text| Term::new(text).map_err(|err| PyValueError::new_err(err.to_string())))
        .collect()
}

/// One candidate row. Rows from the memory index borrow their text; rows from
/// Python or SQLite, and every kept hit, own it.
#[derive(Clone, Debug)]
pub(crate) struct Candidate<'a> {
    pub path_text: Cow<'a, str>,
    pub identity: Cow<'a, str>,
    pub kind: Kind,
    pub search_name: Cow<'a, str>,
    pub search_path: Cow<'a, str>,
    pub extension: Cow<'a, str>,
    pub size: i64,
    pub modified: f64,
    pub attributes: i64,
    pub launches: i64,
    pub last_used: f64,
    pub query_launches: i64,
    pub pinned: bool,
    /// Indexed from a Start-menu folder or App Paths; exempt from file-name patterns.
    pub application_source: bool,
}

impl Candidate<'_> {
    pub fn from_python(row: &Bound<'_, PyAny>) -> PyResult<Candidate<'static>> {
        let row = row.downcast::<PyTuple>()?;
        if row.len() != CANDIDATE_COLUMNS {
            return Err(PyValueError::new_err(format!(
                "Expected a {CANDIDATE_COLUMNS}-column candidate row"
            )));
        }
        let kind: String = row.get_item(2)?.extract()?;
        let text = |index: usize| -> PyResult<Cow<'static, str>> {
            Ok(Cow::Owned(row.get_item(index)?.extract()?))
        };
        let candidate = Candidate {
            path_text: text(0)?,
            identity: text(1)?,
            kind: Kind::parse(&kind)?,
            search_name: text(3)?,
            search_path: text(4)?,
            extension: text(5)?,
            size: row.get_item(6)?.extract()?,
            modified: row.get_item(7)?.extract()?,
            attributes: row.get_item(8)?.extract()?,
            launches: row.get_item(9)?.extract()?,
            last_used: row.get_item(10)?.extract()?,
            query_launches: row.get_item(11)?.extract()?,
            pinned: row.get_item(12)?.is_truthy()?,
            application_source: row.get_item(13)?.is_truthy()?,
        };
        candidate.validate()?;
        Ok(candidate)
    }

    /// A copy that owns its text, so it can outlive the memory index it came from.
    pub fn into_owned(self) -> Candidate<'static> {
        Candidate {
            path_text: Cow::Owned(self.path_text.into_owned()),
            identity: Cow::Owned(self.identity.into_owned()),
            kind: self.kind,
            search_name: Cow::Owned(self.search_name.into_owned()),
            search_path: Cow::Owned(self.search_path.into_owned()),
            extension: Cow::Owned(self.extension.into_owned()),
            size: self.size,
            modified: self.modified,
            attributes: self.attributes,
            launches: self.launches,
            last_used: self.last_used,
            query_launches: self.query_launches,
            pinned: self.pinned,
            application_source: self.application_source,
        }
    }

    pub fn validate(&self) -> PyResult<()> {
        if !self.modified.is_finite()
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

    /// `PurePath.stem`: a leading or trailing dot is no suffix.
    pub fn stem(&self) -> &str {
        let name = self.file_name();
        match name.rfind('.') {
            Some(dot) if dot > 0 && dot < name.len() - 1 => &name[..dot],
            _ => name,
        }
    }

    /// The display name: applications omit their extension.
    pub fn name(&self) -> &str {
        if self.kind == Kind::App {
            self.stem()
        } else {
            self.file_name()
        }
    }

    pub fn to_python(&self, py: Python<'_>) -> PyResult<Py<PyTuple>> {
        let fields: [PyObject; CANDIDATE_COLUMNS] = [
            (&*self.path_text).into_pyobject(py)?.into_any().unbind(),
            (&*self.identity).into_pyobject(py)?.into_any().unbind(),
            self.kind.as_str().into_pyobject(py)?.into_any().unbind(),
            (&*self.search_name).into_pyobject(py)?.into_any().unbind(),
            (&*self.search_path).into_pyobject(py)?.into_any().unbind(),
            (&*self.extension).into_pyobject(py)?.into_any().unbind(),
            self.size.into_pyobject(py)?.into_any().unbind(),
            self.modified.into_pyobject(py)?.into_any().unbind(),
            self.attributes.into_pyobject(py)?.into_any().unbind(),
            self.launches.into_pyobject(py)?.into_any().unbind(),
            self.last_used.into_pyobject(py)?.into_any().unbind(),
            self.query_launches.into_pyobject(py)?.into_any().unbind(),
            self.pinned
                .into_pyobject(py)?
                .to_owned()
                .into_any()
                .unbind(),
            self.application_source
                .into_pyobject(py)?
                .to_owned()
                .into_any()
                .unbind(),
        ];
        Ok(PyTuple::new(py, fields)?.unbind())
    }
}

/// Queries and rows for unit tests, with neutral defaults.
#[cfg(test)]
pub(crate) mod testing {
    use super::*;

    pub fn query(terms: &[&str]) -> Query {
        Query {
            terms: terms
                .iter()
                .map(|text| Term::new((*text).into()).unwrap())
                .collect(),
            folder_parts: Vec::new(),
            excluded_terms: Vec::new(),
            kind: None,
            extensions: Vec::new(),
            excluded_extensions: Vec::new(),
            scope_key: None,
            scope_search_prefix: None,
            modified_after: None,
            modified_before: None,
            min_size: None,
            max_size: None,
            content_text: None,
            content_search_threads: 1,
            explorer: false,
            basename_only: false,
            fuzzy: true,
            combined: false,
            sort: SortOrder::Score,
            limit: 10,
            now: 0.0,
        }
    }

    /// A file row at `path`, normalized as the catalog stores it.
    pub fn file(path: &str) -> Candidate<'static> {
        let name = path.rsplit('/').next().unwrap_or(path);
        let extension = name.rsplit_once('.').map_or("", |(_, extension)| extension);
        Candidate {
            path_text: path.to_string().into(),
            identity: path.to_uppercase().into(),
            kind: Kind::File,
            search_name: name.to_lowercase().into(),
            search_path: path.to_lowercase().into(),
            extension: extension.to_lowercase().into(),
            size: 0,
            modified: 0.0,
            attributes: 0,
            launches: 0,
            last_used: 0.0,
            query_launches: 0,
            pinned: false,
            application_source: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{file, query};
    use super::*;

    fn terms(texts: &[&str]) -> Vec<Term> {
        compile_terms(texts.iter().map(|text| (*text).to_string()).collect()).unwrap()
    }

    #[test]
    fn excluded_extensions_reject_names_and_patterns_but_keep_others() {
        let mut excluding = query(&[]);
        excluding.excluded_extensions = terms(&["tmp", "b?k"]);
        assert!(!excluding.accepts(&file("C:/Work/draft.tmp")));
        assert!(!excluding.accepts(&file("C:/Work/draft.bak")));
        assert!(excluding.accepts(&file("C:/Work/draft.txt")));
        assert!(excluding.accepts(&file("C:/Work/tmp/draft")));
    }

    #[test]
    fn exclusions_win_over_an_including_extension_filter() {
        let mut both = query(&[]);
        both.extensions = terms(&["t*"]);
        both.excluded_extensions = terms(&["tmp"]);
        assert!(both.accepts(&file("C:/Work/notes.txt")));
        assert!(!both.accepts(&file("C:/Work/notes.tmp")));
        assert!(!both.accepts(&file("C:/Work/notes.md")));
    }
}
