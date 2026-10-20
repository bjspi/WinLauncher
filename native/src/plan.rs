//! Candidate plans: Python's candidate SQL conditions, evaluated against the memory index.
//!
//! Python's `_CandidateFilter` builds every WHERE condition twice, as SQL and as
//! a plan node, so both engines select exactly the same candidates. Nodes are
//! tuples whose first item names the operation; see `Condition::from_python`.

use std::cmp::Ordering;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

use crate::catalog::{CatalogIndex, Column};
use crate::like::LikePattern;
use crate::query::required;

fn invalid(message: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(format!("Invalid candidate plan: {message}"))
}

#[derive(Clone, Copy, Debug)]
enum Operator {
    Equal,
    NotEqual,
    Less,
    LessOrEqual,
    Greater,
    GreaterOrEqual,
}

impl Operator {
    fn parse(text: &str) -> PyResult<Self> {
        Ok(match text {
            "=" => Self::Equal,
            "!=" => Self::NotEqual,
            "<" => Self::Less,
            "<=" => Self::LessOrEqual,
            ">" => Self::Greater,
            ">=" => Self::GreaterOrEqual,
            _ => return Err(invalid(format!("unknown operator {text:?}"))),
        })
    }

    fn holds(self, ordering: Ordering) -> bool {
        match self {
            Self::Equal => ordering.is_eq(),
            Self::NotEqual => ordering.is_ne(),
            Self::Less => ordering.is_lt(),
            Self::LessOrEqual => ordering.is_le(),
            Self::Greater => ordering.is_gt(),
            Self::GreaterOrEqual => ordering.is_ge(),
        }
    }
}

/// A comparison operand; SQLite compares integers and reals by value.
#[derive(Debug)]
enum Operand {
    Text(String),
    Number(f64),
}

/// One plan node.
#[derive(Debug)]
enum Condition {
    Like(Column, LikePattern),
    Compare(Column, Operator, Operand),
    In(Column, Vec<String>),
    Any(Vec<Condition>),
    NotAll(Vec<Condition>),
    Learned,
}

/// What a condition needs from the current search beyond the entry itself.
pub(crate) trait Context {
    fn is_learned(&self, entry: usize) -> bool;
}

impl Condition {
    fn from_python(node: &Bound<'_, PyAny>) -> PyResult<Self> {
        let node = node.downcast::<PyTuple>()?;
        let operation: String = node.get_item(0)?.extract()?;
        let column = |index: usize| -> PyResult<Column> {
            let name: String = node.get_item(index)?.extract()?;
            Column::parse(&name).ok_or_else(|| invalid(format!("unknown column {name:?}")))
        };
        let children = |index: usize| -> PyResult<Vec<Condition>> {
            node.get_item(index)?
                .try_iter()?
                .map(|child| Self::from_python(&child?))
                .collect()
        };
        let condition = match (operation.as_str(), node.len()) {
            ("like", 3) => {
                let column = column(1)?;
                if !column.is_text() {
                    return Err(invalid("LIKE needs a text column"));
                }
                Self::Like(
                    column,
                    LikePattern::new(&node.get_item(2)?.extract::<String>()?),
                )
            }
            ("compare", 4) => {
                let column = column(1)?;
                let operator = Operator::parse(&node.get_item(2)?.extract::<String>()?)?;
                let value = node.get_item(3)?;
                let operand = if column.is_text() {
                    Operand::Text(value.extract()?)
                } else {
                    Operand::Number(value.extract()?)
                };
                Self::Compare(column, operator, operand)
            }
            ("in", 3) => {
                let column = column(1)?;
                if !column.is_text() {
                    return Err(invalid("IN needs a text column"));
                }
                Self::In(column, node.get_item(2)?.extract()?)
            }
            ("any", 2) => Self::Any(children(1)?),
            ("not_all", 2) => Self::NotAll(children(1)?),
            ("learned", 1) => Self::Learned,
            _ => return Err(invalid(format!("unexpected {operation:?} node"))),
        };
        Ok(condition)
    }

    fn holds(&self, index: &CatalogIndex, entry: usize, context: &impl Context) -> bool {
        match self {
            Self::Like(column, pattern) => pattern.matches(index.text(*column, entry)),
            Self::Compare(column, operator, Operand::Text(value)) => {
                operator.holds(index.text(*column, entry).cmp(value.as_str()))
            }
            Self::Compare(column, operator, Operand::Number(value)) => index
                .number(*column, entry)
                .partial_cmp(value)
                .is_some_and(|ordering| operator.holds(ordering)),
            Self::In(column, values) => {
                let text = index.text(*column, entry);
                values.iter().any(|value| value == text)
            }
            Self::Any(conditions) => conditions
                .iter()
                .any(|condition| condition.holds(index, entry, context)),
            Self::NotAll(conditions) => !conditions
                .iter()
                .all(|condition| condition.holds(index, entry, context)),
            Self::Learned => context.is_learned(entry),
        }
    }
}

/// The conditions of one candidate pass; all of them must hold.
#[derive(Debug)]
pub(crate) struct Pass {
    conditions: Vec<Condition>,
}

impl Pass {
    pub fn holds(&self, index: &CatalogIndex, entry: usize, context: &impl Context) -> bool {
        self.conditions
            .iter()
            .all(|condition| condition.holds(index, entry, context))
    }
}

/// Both passes of one search, as Python's `memory_plan` sends them.
#[derive(Debug)]
pub(crate) struct Plan {
    pub literal: Pass,
    pub fuzzy: Option<Pass>,
    pub history_key: String,
}

impl Plan {
    pub fn from_python(plan: &Bound<'_, PyDict>) -> PyResult<Self> {
        let pass = |value: Bound<'_, PyAny>| -> PyResult<Pass> {
            let conditions = value
                .try_iter()?
                .map(|node| Condition::from_python(&node?))
                .collect::<PyResult<_>>()?;
            Ok(Pass { conditions })
        };
        let literal = pass(required(plan, "literal")?)?;
        let fuzzy: Option<Bound<'_, PyAny>> = required(plan, "fuzzy")?;
        let fuzzy = fuzzy.map(pass).transpose()?;
        Ok(Self {
            literal,
            fuzzy,
            history_key: required(plan, "history_key")?,
        })
    }
}
